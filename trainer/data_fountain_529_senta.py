from paddlenlp.transformers import PretrainedTokenizer, LinearDecayWithWarmup
from paddlenlp.datasets import MapDataset
from paddlenlp.data import Stack, Tuple, Pad
from paddle import nn
from dotmap import DotMap
from functools import partial
from visualdl import LogWriter
from utils.utils import create_data_loader, mkdir_if_not_exist
from utils.metric import Kappa
from utils.loss import FocalLoss
from utils.adversarial import FGM
import paddle.nn.functional as F
import numpy as np
import time
import paddle
import os


class DataFountain529SentaTrainer(object):
    train_data_loader = None
    dev_data_loader = None
    epochs = None
    ckpt_dir = None
    num_training_steps = None
    optimizer = None
    criterion = None
    metric = None

    def __init__(self,
                 model: nn.Layer,
                 tokenizer: PretrainedTokenizer,
                 train_ds: MapDataset,
                 dev_ds: MapDataset,
                 config: DotMap):
        if config.model_name not in config.model_config:
            raise RuntimeError("current model: {} not configured".format(config.model_name))
        self.model = model
        self.tokenizer = tokenizer
        self.train_ds = train_ds
        self.dev_ds = dev_ds
        self.logger = config.logger
        self.config = config
        self._gen_data_loader(config)
        self._prepare(config)

    def _gen_data_loader(self, config: DotMap):
        # 将数据处理成模型可读入的数据格式
        trans_func = partial(
            self.convert_example,
            tokenizer=self.tokenizer,
            max_seq_len=config.max_seq_len)

        # 将数据组成批量式数据，如
        # 将不同长度的文本序列 padding 到批量式数据中最大长度
        # 将每条数据 label 堆叠在一起
        batchify_fn = lambda samples, fn=Tuple(
            Pad(axis=0, pad_val=self.tokenizer.pad_token_id),  # input_ids
            Pad(axis=0, pad_val=self.tokenizer.pad_token_type_id),  # token_type_ids
            Stack()  # labels
        ): [data for data in fn(samples)]

        self.train_data_loader = create_data_loader(
            self.train_ds,
            trans_fn=trans_func,
            mode='train',
            batch_size=config.batch_size,
            batchify_fn=batchify_fn)
        self.dev_data_loader = create_data_loader(
            self.dev_ds,
            trans_fn=trans_func,
            mode='dev',
            batch_size=config.batch_size,
            batchify_fn=batchify_fn)

    def _prepare(self, config: DotMap):
        # 当前训练折次
        self.fold = config.fold
        # 训练折数
        self.total_fold = config.k_fold or 1
        # 训练轮次
        self.epochs = config.epochs

        # 保存模型参数的文件夹
        self.ckpt_dir = os.path.join(config.ckpt_dir, config.model_name, "fold_{}".format(self.fold))
        mkdir_if_not_exist(self.ckpt_dir)

        # 可视化日志的文件夹
        self.train_vis_dir = os.path.join(config.vis_dir, config.model_name, "fold_{}/train".format(self.fold))
        self.dev_vis_dir = os.path.join(config.vis_dir, config.model_name, "fold_{}/dev".format(self.fold))
        mkdir_if_not_exist(self.train_vis_dir)
        mkdir_if_not_exist(self.dev_vis_dir)

        # 训练所需要的总 step 数
        self.num_training_steps = len(self.train_data_loader) * self.epochs

        # 定义 learning_rate_scheduler，负责在训练过程中对 lr 进行调度
        self.lr_scheduler = LinearDecayWithWarmup(config.learning_rate, self.num_training_steps, 0.0)

        # Generate parameter names needed to perform weight decay.
        # All bias and LayerNorm parameters are excluded.
        decay_params = [
            p.name for n, p in self.model.named_parameters()
            if not any(nd in n for nd in ["bias", "norm"])
        ]
        # 定义 Optimizer
        self.optimizer = paddle.optimizer.AdamW(
            learning_rate=self.lr_scheduler,
            parameters=self.model.parameters(),
            weight_decay=0.0,
            apply_decay_param_fun=lambda x: x in decay_params)

        self.enable_adversarial = False
        if config.adversarial:
            self.enable_adversarial = True
            if config.adversarial == "fgm":
                self.adv = FGM(self.model, epsilon=1e-5, emb_name="word_embeddings")
            else:
                raise RuntimeError("config error adversarial: {}".format(config.adversarial))

        if config.loss_func == "ce_loss":
            # 交叉熵损失函数
            self.criterion = paddle.nn.loss.CrossEntropyLoss()
            self.eval_criterion = paddle.nn.loss.CrossEntropyLoss()
            self.adv_criterion = paddle.nn.loss.CrossEntropyLoss()
        elif config.loss_func == "focal_loss":
            # Focal Loss
            self.criterion = FocalLoss(num_classes=config.num_classes)
            self.eval_criterion = FocalLoss(num_classes=config.num_classes)
            self.adv_criterion = FocalLoss(num_classes=config.num_classes)
        elif config.loss_func == "focal_loss-gamma3":
            # Focal Loss (gamma = 3)
            self.criterion = FocalLoss(num_classes=config.num_classes, gamma=3)
            self.eval_criterion = FocalLoss(num_classes=config.num_classes, gamma=3)
            self.adv_criterion = FocalLoss(num_classes=config.num_classes, gamma=3)
        elif config.loss_func == "focal_loss-gamma5":
            # Focal Loss (gamma = 5)
            self.criterion = FocalLoss(num_classes=config.num_classes, gamma=5)
            self.eval_criterion = FocalLoss(num_classes=config.num_classes, gamma=5)
            self.adv_criterion = FocalLoss(num_classes=config.num_classes, gamma=5)
        else:
            raise RuntimeError("config error loss function: {}".format(config.loss_func))

        # Kappa 评价指标
        self.metric = Kappa(config.num_classes)
        self.eval_metric = Kappa(config.num_classes)

        # Acc 评价指标
        self.acc_metric = paddle.metric.Accuracy()
        self.eval_acc_metric = paddle.metric.Accuracy()

    def train(self):
        # 开启训练
        global_step = 0
        tic_train = time.time()

        with LogWriter(logdir=self.train_vis_dir) as train_writer:
            with LogWriter(logdir=self.dev_vis_dir) as dev_writer:
                for epoch in range(1, self.epochs + 1):
                    for step, batch in enumerate(self.train_data_loader, start=1):
                        input_ids, token_type_ids, labels = batch

                        # 喂数据给 model
                        logits = self.model(input_ids, token_type_ids)

                        # 计算损失函数值
                        loss = self.criterion(logits, labels)

                        # 反向梯度回传，更新参数
                        loss.backward()

                        # 预测分类概率值
                        probs = F.softmax(logits, axis=1)
                        preds = paddle.argmax(probs, axis=1, keepdim=True)

                        # 计算 kappa
                        self.metric.update(preds, labels)
                        kappa = self.metric.accumulate()

                        # 计算 acc
                        correct = self.acc_metric.compute(probs, labels)
                        self.acc_metric.update(correct)
                        acc = self.acc_metric.accumulate()

                        global_step += 1
                        self.logger.info(
                            "「%d/%d」global step %d, epoch: %d, batch: %d, loss: %.5f, kappa: %.5f, acc: %.5f, speed: %.2f step/s"
                            % (self.fold, self.total_fold - 1, global_step, epoch, step, loss, kappa, acc, 1 / (time.time() - tic_train)))
                        tic_train = time.time()

                        train_writer.add_scalar(tag="kappa", step=global_step, value=kappa)
                        train_writer.add_scalar(tag="acc", step=global_step, value=acc)
                        train_writer.add_scalar(tag="loss", step=global_step, value=loss)

                        # 对抗训练
                        if self.enable_adversarial:
                            self.adv.attack()  # 在 embedding 上添加对抗扰动
                            adv_logits = self.model(input_ids, token_type_ids)
                            adv_loss = self.adv_criterion(adv_logits, labels)
                            adv_loss.backward()  # 反向传播，并在正常的 grad 基础上，累加对抗训练的梯度
                            self.adv.restore()  # 恢复 embedding 参数

                        self.optimizer.step()
                        self.lr_scheduler.step()
                        self.optimizer.clear_grad()

                        if global_step % 10 == 0 or global_step == self.num_training_steps:
                            # 评估当前训练的模型
                            loss_dev, kappa_dev, acc_dev = self.evaluate()

                            dev_writer.add_scalar(tag="kappa", step=global_step, value=kappa_dev)
                            dev_writer.add_scalar(tag="acc", step=global_step, value=acc_dev)
                            dev_writer.add_scalar(tag="loss", step=global_step, value=loss_dev)

                            # # 保存当前模型参数等
                            # if global_step >= 100:
                            #     save_dir = os.path.join(self.ckpt_dir, "model_%d" % global_step)
                            #     mkdir_if_not_exist(save_dir)
                            #     paddle.save(self.model.state_dict(), os.path.join(save_dir, "model.pdparams"))

    @staticmethod
    def convert_example(example, tokenizer, max_seq_len=512):
        encoded_inputs = tokenizer(text=example["text"], max_seq_len=max_seq_len, pad_to_max_seq_len=True)
        return tuple([np.array(x, dtype="int64") for x in [
            encoded_inputs["input_ids"], encoded_inputs["token_type_ids"], [example["label"]]]])

    @paddle.no_grad()
    def evaluate(self):
        self.model.eval()
        self.eval_metric.reset()
        self.eval_acc_metric.reset()
        losses, kappa, acc = [], 0.0, 0.0
        for batch in self.dev_data_loader:
            input_ids, token_type_ids, labels = batch
            logits = self.model(input_ids, token_type_ids)

            loss = self.eval_criterion(logits, labels)
            losses.append(loss.numpy())

            probs = F.softmax(logits, axis=1)
            preds = paddle.argmax(probs, axis=1, keepdim=True)

            self.eval_metric.update(preds, labels)
            kappa = self.eval_metric.accumulate()

            correct = self.eval_acc_metric.compute(probs, labels)
            self.eval_acc_metric.update(correct)
            acc = self.eval_acc_metric.accumulate()

        self.logger.info("「%d/%d」eval loss: %.5f, kappa: %.5f, acc: %.5f"
                         % (self.fold, self.total_fold - 1, float(np.mean(losses)), kappa, acc))
        self.model.train()
        self.eval_metric.reset()
        self.eval_acc_metric.reset()

        return float(np.mean(losses)), kappa, acc
