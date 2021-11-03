from paddlenlp.transformers import BertTokenizer
from data_process.data_fountain_529_senta import DataFountain529SentaDataProcessor
from dataset.data_fountain_529_senta import DataFountain529SentaDataset
from model.data_fountain_529_senta import DataFountain529SentaBertBaselineModel
from infer.data_fountain_529_senta import DataFountain529SentaInfer
from utils.config_utils import get_config, CONFIG_PATH
from utils.utils import mkdir_if_not_exist
from bunch import Bunch
import csv
import os


def predict():
    config = get_config(os.path.join(CONFIG_PATH, "data_fountain_529_senta.json"))

    # 原始数据预处理
    data_processor = DataFountain529SentaDataProcessor(config)
    data_processor.process()
    config = data_processor.config

    k_fold_result = []
    k_fold_models = {
        1: "model_300",
        2: "model_500",
        3: "model_900",
        4: "model_600",
        5: "model_300"
    }
    for fold in range(1, config.k_fold + 1):
        # 获取测试集
        [test_ds] = DataFountain529SentaDataset(config).load_data(splits=['test'], lazy=False)

        # 加载 model 和 tokenizer
        model, tokenizer = get_model_and_tokenizer(config.model_name, config)

        # 获取推断器
        config.model_path = os.path.join(config.ckpt_dir, config.model_name, k_fold_models[fold])
        infer = DataFountain529SentaInfer(model, tokenizer=tokenizer, test_ds=test_ds, config=config)

        # 开始预测
        fold_result = infer.predict()
        k_fold_result.append(fold_result)

    # 融合 k 折模型的预测结果
    result = merge_k_fold_result(k_fold_result)

    # 写入预测结果
    res_dir = os.path.join(config.res_dir, config.model_name)
    mkdir_if_not_exist(res_dir)
    with open(os.path.join(res_dir, "result.csv"), "w", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "class"])
        for line in result:
            qid, label = line
            writer.writerow([qid, label])


def merge_k_fold_result(k_fold_result):
    k, n = len(k_fold_result), len(k_fold_result[0])
    result = []
    for i in range(n):
        qid = k_fold_result[0][i][0][0]
        labels = {}
        for j in range(k):
            label = k_fold_result[j][i][1]
            labels[label] = labels.get(label, 0) + 1
        labels = sorted(labels.items(), key=lambda x: x[1], reverse=True)
        label = labels[0][0] if labels[0][1] > k // 2 else 2
        result.append([qid, label])
    return result


def get_model_and_tokenizer(model_name: str, config: Bunch):
    model, tokenizer = None, None
    logger = config.logger
    if model_name == "bert_baseline":
        model = DataFountain529SentaBertBaselineModel.from_pretrained("bert-wwm-chinese", config=config)
        tokenizer = BertTokenizer.from_pretrained("bert-wwm-chinese")
    else:
        logger.error("load model error: {}.".format(model_name))
    return model, tokenizer


if __name__ == "__main__":
    predict()
