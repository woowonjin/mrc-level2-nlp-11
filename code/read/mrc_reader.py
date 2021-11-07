from transformers import (
    AutoConfig,
    AutoModelForQuestionAnswering,
    AutoTokenizer,
    EvalPrediction,
    DataCollatorWithPadding,
)
from utils_qa import postprocess_qa_predictions
from importlib import import_module
import sys
from arguments import (
    ModelArguments,
    DataTrainingArguments,
)
from typing import List, Callable, NoReturn, NewType, Any
import dataclasses
from datasets import load_metric, Dataset, DatasetDict


class Reader:
    """
    Get pretrained_model from HugginFace
    Get custom_model from ./model
    'custom_model' 파라미터 필요
    ...
    Attributes
    -----------
    model_name : str
        pre/custom_modelName
    tokenizer_name : str
        default = None -> It will be same with model_name
    config_name : str
        default = None -> It will be same with model_name
    params : dict
        custom_model param (default=None)
        # to be implemented
    Methods
    --------
    set_model_and_tokenizer(): -> None
        The method for setting model

    get() -> (AutoModelForQuestionAnswering, AutoTokenizer)
        The method for getting model and tokenizer
    """

    get_custom_class = {"custom1": "CustomRobertaLarge",
                        "custom2": "CustomRobertaLarge", "custom3": "CustomRobertaLarge", }

    def __init__(
        self,
        model_args: ModelArguments,
        data_args: DataTrainingArguments,
        datasets: DatasetDict,
        params: dict = None,
    ):
        self.classifier = model_args.model_name_or_path.split("_")[0]
        self.model_name = model_args.model_name_or_path.split("_")[1]
        self.tokenizer_name = model_args.tokenizer_name
        self.config_name = model_args.config_name

        self.data_args = data_args

        self.params = params
        self.set_model_and_tokenizer()
        self.datasets = datasets

    def set_model_and_tokenizer(self) -> NoReturn:
        # Issue : # klue/bert-base, pre_klue/bert-base -> naming convention이 불편하다
        if self.classifier == "pre":
            model_config = AutoConfig.from_pretrained(
                self.config_name if self.config_name is not None else self.model_name,
            )
            model_tokenizer = AutoTokenizer.from_pretrained(
                self.tokenizer_name
                if self.tokenizer_name is not None
                else self.model_name,
                # 'use_fast' argument를 True로 설정할 경우 rust로 구현된 tokenizer를 사용할 수 있습니다.
                # False로 설정할 경우 python으로 구현된 tokenizer를 사용할 수 있으며,
                # rust version이 비교적 속도가 빠릅니다.
                use_fast=True,
            )
            model = AutoModelForQuestionAnswering.from_pretrained(
                self.model_name,
                from_tf=bool(".ckpt" in self.model_name),
                config=model_config,
            )
            self.model = model
            self.tokenizer = model_tokenizer
        elif self.classifier == "custom":
            sys.path.append("./models")
            # Custom_model일경우 model_name.py에서 tokenizer, config도 받아와야한다.
            model_module = getattr(
                import_module(
                    self.model_name), self.get_custom_class[self.model_name]
            )
            self.model = model_module()
            self.tokenizer = self.model.get_tokenizer()
        else:
            print("잘못된 이름 또는 없는 모델입니다.")

    def get_model_tokenizer(self) -> (AutoModelForQuestionAnswering, AutoTokenizer):
        return self.model, self.tokenizer

    def set_column_name(self, do_train: bool) -> NoReturn:
        if do_train:
            self.column_names = self.datasets["train"].column_names
        else:
            self.column_names = self.datasets["validation"].column_names

        self.question_column_name = (
            "question" if "question" in self.column_names else self.column_names[0]
        )
        self.context_column_name = (
            "context" if "context" in self.column_names else self.column_names[1]
        )
        self.answer_column_name = (
            "answers" if "answers" in self.column_names else self.column_names[2]
        )

    def set_max_seq_length(self, max_seq_length: int) -> NoReturn:
        self.max_seq_length = max_seq_length

    def prepare_train_features(self, examples: Dataset):
        # Train preprocessing / 전처리를 진행하는 함수.
        # truncation과 padding(length가 짧을때만)을 통해 toknization을 진행하며, stride를 이용하여 overflow를 유지합니다.
        # 각 example들은 이전의 context와 조금씩 겹치게됩니다.
        tokenizer = self.tokenizer
        data_args = self.data_args

        # Padding에 대한 옵션을 설정합니다.
        # (question|context) 혹은 (context|question)로 세팅 가능합니다.
        pad_on_right = tokenizer.padding_side == "right"

        tokenized_examples = tokenizer(
            examples[
                self.question_column_name if pad_on_right else self.context_column_name
            ],
            examples[
                self.context_column_name if pad_on_right else self.question_column_name
            ],
            truncation="only_second" if pad_on_right else "only_first",
            max_length=self.max_seq_length,
            stride=data_args.doc_stride,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
            # return_token_type_ids=False, # roberta모델을 사용할 경우 False, bert를 사용할 경우 True로 표기해야합니다.
            padding="max_length" if data_args.pad_to_max_length else False,
        )

        # 길이가 긴 context가 등장할 경우 truncate를 진행해야하므로, 해당 데이터셋을 찾을 수 있도록 mapping 가능한 값이 필요합니다.
        sample_mapping = tokenized_examples.pop("overflow_to_sample_mapping")
        # token의 캐릭터 단위 position를 찾을 수 있도록 offset mapping을 사용합니다.
        # start_positions과 end_positions을 찾는데 도움을 줄 수 있습니다.
        offset_mapping = tokenized_examples.pop("offset_mapping")

        # 데이터셋에 "start position", "enc position" label을 부여합니다.
        tokenized_examples["start_positions"] = []
        tokenized_examples["end_positions"] = []

        for i, offsets in enumerate(offset_mapping):
            input_ids = tokenized_examples["input_ids"][i]
            cls_index = input_ids.index(tokenizer.cls_token_id)  # cls index

            # sequence id를 설정합니다 (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)

            # 하나의 example이 여러개의 span을 가질 수 있습니다.
            sample_index = sample_mapping[i]
            answers = examples[self.answer_column_name][sample_index]

            # answer가 없을 경우 cls_index를 answer로 설정합니다(== example에서 정답이 없는 경우 존재할 수 있음).
            if len(answers["answer_start"]) == 0:
                tokenized_examples["start_positions"].append(cls_index)
                tokenized_examples["end_positions"].append(cls_index)
            else:
                # text에서 정답의 Start/end character index
                start_char = answers["answer_start"][0]
                end_char = start_char + len(answers["text"][0])

                # text에서 current span의 Start token index
                token_start_index = 0
                while sequence_ids[token_start_index] != (1 if pad_on_right else 0):
                    token_start_index += 1

                # text에서 current span의 End token index
                token_end_index = len(input_ids) - 1
                while sequence_ids[token_end_index] != (1 if pad_on_right else 0):
                    token_end_index -= 1

                # 정답이 span을 벗어났는지 확인합니다(정답이 없는 경우 CLS index로 label되어있음).
                if not (
                    offsets[token_start_index][0] <= start_char
                    and offsets[token_end_index][1] >= end_char
                ):
                    tokenized_examples["start_positions"].append(cls_index)
                    tokenized_examples["end_positions"].append(cls_index)
                else:
                    # token_start_index 및 token_end_index를 answer의 끝으로 이동합니다.
                    # Note: answer가 마지막 단어인 경우 last offset을 따라갈 수 있습니다(edge case).
                    while (
                        token_start_index < len(offsets)
                        and offsets[token_start_index][0] <= start_char
                    ):
                        token_start_index += 1
                    tokenized_examples["start_positions"].append(
                        token_start_index - 1)
                    while offsets[token_end_index][1] >= end_char:
                        token_end_index -= 1
                    tokenized_examples["end_positions"].append(
                        token_end_index + 1)

        return tokenized_examples

    def get_train_dataset(self) -> Dataset:
        train_dataset = self.datasets["train"]

        train_dataset = train_dataset.map(
            self.prepare_train_features,
            batched=True,
            num_proc=self.data_args.preprocessing_num_workers,
            remove_columns=self.column_names,
            load_from_cache_file=not self.data_args.overwrite_cache,
        )
        return train_dataset

    def prepare_validation_features(self, examples: Dataset):
        # truncation과 padding(length가 짧을때만)을 통해 toknization을 진행하며, stride를 이용하여 overflow를 유지합니다.
        # 각 example들은 이전의 context와 조금씩 겹치게됩니다.
        tokenizer = self.tokenizer
        pad_on_right = tokenizer.padding_side == "right"

        tokenized_examples = tokenizer(
            examples[
                self.question_column_name if pad_on_right else self.context_column_name
            ],
            examples[
                self.context_column_name if pad_on_right else self.question_column_name
            ],
            truncation="only_second" if pad_on_right else "only_first",
            max_length=self.max_seq_length,
            stride=self.data_args.doc_stride,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
            # return_token_type_ids=False, # roberta모델을 사용할 경우 False, bert를 사용할 경우 True로 표기해야합니다.
            padding="max_length" if self.data_args.pad_to_max_length else False,
        )

        # 길이가 긴 context가 등장할 경우 truncate를 진행해야하므로, 해당 데이터셋을 찾을 수 있도록 mapping 가능한 값이 필요합니다.
        sample_mapping = tokenized_examples.pop("overflow_to_sample_mapping")

        # evaluation을 위해, prediction을 context의 substring으로 변환해야합니다.
        # corresponding example_id를 유지하고 offset mappings을 저장해야합니다.
        tokenized_examples["example_id"] = []

        for i in range(len(tokenized_examples["input_ids"])):
            # sequence id를 설정합니다 (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)
            context_index = 1 if pad_on_right else 0

            # 하나의 example이 여러개의 span을 가질 수 있습니다.
            sample_index = sample_mapping[i]
            tokenized_examples["example_id"].append(
                examples["id"][sample_index])

            # Set to None the offset_mapping을 None으로 설정해서 token position이 context의 일부인지 쉽게 판별 할 수 있습니다.
            tokenized_examples["offset_mapping"][i] = [
                (o if sequence_ids[k] == context_index else None)
                for k, o in enumerate(tokenized_examples["offset_mapping"][i])
            ]

        return tokenized_examples

    def get_validation_dataset(self) -> Dataset:
        eval_dataset = self.datasets["validation"]

        # Validation Feature 생성
        eval_dataset = eval_dataset.map(
            self.prepare_validation_features,
            batched=True,
            num_proc=self.data_args.preprocessing_num_workers,
            remove_columns=self.column_names,
            load_from_cache_file=not self.data_args.overwrite_cache,
        )
        return eval_dataset


if __name__ == "__main__":
    # model = Reader("custom_testmodel", params={"layer":30, "classNum":20}).get() # 통과
    reader = Reader("pre_klue/bert-base")
    model, tokenizer = reader.get()
    print(model, tokenizer)
