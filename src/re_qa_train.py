import argparse
import csv
import io
import json
import math
import os
import random
import re
import string
import time
from collections import Counter
from configparser import ConfigParser
from pathlib import Path
from typing import Generator, Optional

import datasets
import numpy as np
import pandas as pd
import torch
from datasets import load_dataset
from torch.utils.data import DataLoader

from src.nq_utils import (create_narrative_dataset,
                          create_reverse_narrative_dataset)
from src.t5_model import T5QA, HyperParameters


def white_space_fix(text):
    return " ".join(text.split())


def read_squad(path):
    path = Path(path)
    with open(path, "rb") as f:
        squad_dict = json.load(f)

    contexts = []
    answers = []
    for group in squad_dict["data"]:
        for passage in group["paragraphs"]:
            context = passage["context"]
            for qa in passage["qas"]:
                question = qa["question"]
                if qa["answers"]:
                    answ = random.choice(qa["answers"])
                    contexts.append(
                        "question: "
                        + white_space_fix(question)
                        + " context: "
                        + white_space_fix(context)
                        + " </s>"
                    )
                    answers.append(white_space_fix(answ["text"]) + " </s>")
                else:
                    contexts.append(
                        "question: "
                        + white_space_fix(question)
                        + " context: "
                        + white_space_fix(context)
                        + " </s>"
                    )
                    answers.append("no no answer </s>")

    return contexts, answers


def read_reverse_squad(path):
    path = Path(path)
    with open(path, "rb") as f:
        squad_dict = json.load(f)

    contexts = []
    answers = []
    for group in squad_dict["data"]:
        for passage in group["paragraphs"]:
            context = passage["context"]
            for qa in passage["qas"]:
                question = qa["question"]
                if qa["answers"]:
                    answ = random.choice(qa["answers"])
                    contexts.append(
                        "answer: "
                        + white_space_fix(answ["text"])
                        + " context: "
                        + white_space_fix(context)
                        + " </s>"
                    )
                    answers.append(white_space_fix(question) + " </s>")
                else:
                    contexts.append(
                        "answer: "
                        + "no no answer"
                        + " context: "
                        + white_space_fix(context)
                        + " </s>"
                    )
                    answers.append(white_space_fix(question) + " </s>")

    return contexts, answers


def create_race_dataset(tokenizer, batch_size, source_max_length, decoder_max_length):
    """Function to create the race dataset."""

    def process_race_row(row):
        """Helper function."""
        option_code = row["answer"]
        if option_code == "A":
            option_idx = 0
        elif option_code == "B":
            option_idx = 1
        elif option_code == "C":
            option_idx = 2
        elif option_code == "D":
            option_idx = 3

        answer = white_space_fix(row["options"][option_idx])

        question = white_space_fix(row["question"])

        article = white_space_fix(row["article"])

        return {
            "article": "question: " + question + " context: " + article + " </s>",
            "answer": answer + " </s>",
        }

    def process_data_to_model_inputs(batch):
        # tokenize the inputs and labels
        inputs = tokenizer(
            batch["article"],
            truncation=True,
            padding="max_length",
            max_length=source_max_length,
            add_special_tokens=False,
        )
        outputs = tokenizer(
            batch["answer"],
            truncation=True,
            padding="max_length",
            max_length=decoder_max_length,
            add_special_tokens=False,
        )

        batch["input_ids"] = inputs.input_ids
        batch["attention_mask"] = inputs.attention_mask

        batch["target_ids"] = outputs.input_ids
        batch["target_attention_mask"] = outputs.attention_mask
        batch["labels"] = outputs.input_ids.copy()

        # because BERT automatically shifts the labels, the labels correspond exactly to `target_ids`.
        # We have to make sure that the PAD token is ignored

        labels = [
            [-100 if token == tokenizer.pad_token_id else token for token in labels]
            for labels in batch["labels"]
        ]
        batch["labels"] = labels

        return batch

    train_dataset = load_dataset("race", "all", split="train")
    train_dataset = train_dataset.map(
        process_race_row,
        remove_columns=["options", "example_id"],
    )
    train_dataset = train_dataset.map(
        process_data_to_model_inputs,
        batched=True,
        batch_size=batch_size,
        remove_columns=["answer", "article"],
    )
    train_dataset.set_format(
        type="torch",
        columns=[
            "input_ids",
            "attention_mask",
            "target_ids",
            "target_attention_mask",
            "labels",
        ],
    )

    dev_dataset = load_dataset("race", "all", split="validation")
    dev_dataset = dev_dataset.map(
        process_race_row,
        remove_columns=["options", "example_id"],
    )
    dev_dataset = dev_dataset.map(
        process_data_to_model_inputs,
        batched=True,
        batch_size=batch_size,
        remove_columns=["answer", "article"],
    )
    dev_dataset.set_format(
        type="torch",
        columns=[
            "input_ids",
            "attention_mask",
            "target_ids",
            "target_attention_mask",
            "labels",
        ],
    )
    test_dataset = load_dataset("race", "all", split="test")
    test_dataset = test_dataset.map(
        process_race_row,
        remove_columns=["options", "example_id"],
    )
    test_dataset = test_dataset.map(
        process_data_to_model_inputs,
        batched=True,
        batch_size=batch_size,
        remove_columns=["answer", "article"],
    )
    test_dataset.set_format(
        type="torch",
        columns=[
            "input_ids",
            "attention_mask",
            "target_ids",
            "target_attention_mask",
            "labels",
        ],
    )

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(dev_dataset, batch_size=batch_size, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False)

    return (
        train_loader,
        val_loader,
        test_loader,
        train_dataset,
        dev_dataset,
        test_dataset,
    )


def run_train_epoch(
    model,
    train_dataloader,
) -> Generator:
    """Train the model and return the loss for 'num_steps' given the
    'batch_size' and the train_dataset.

    Randomly pick a batch from the train_dataset.
    """
    step = 0
    for batch in train_dataloader:
        loss_values = model.train(batch)
        step += 1
        yield step, loss_values["loss_value"]


def run_predict(model, dev_dataloader, prediction_file: str) -> None:
    """Read the 'dev_dataset' and predict results with the model, and save the
    results in the prediction_file."""
    writerparams = {"quotechar": '"', "quoting": csv.QUOTE_ALL}
    with io.open(prediction_file, mode="w", encoding="utf-8") as out_fp:
        writer = csv.writer(out_fp, **writerparams)
        header_written = False
        for batch in dev_dataloader:
            for ret_row in model.predict(batch):
                if not header_written:
                    headers = ret_row.keys()
                    writer.writerow(headers)
                    header_written = True
                writer.writerow(list(ret_row.values()))


def save_config(config: HyperParameters, path: str) -> None:
    """Saving config dataclass."""

    config_dict = vars(config)
    parser = ConfigParser()
    parser.add_section("train-parameters")
    for key, value in config_dict.items():
        parser.set("train-parameters", str(key), str(value))
    # save to a file
    with io.open(
        os.path.join(path, "config.ini"), mode="w", encoding="utf-8"
    ) as configfile:
        parser.write(configfile)


def run_model(
    model,
    config,
    train_dataloader=None,
    dev_dataloader=None,
    test_dataloader=None,
    save_always: Optional[bool] = False,
) -> None:
    """Run the model on input data (for training or testing)"""

    model_path = config.model_path
    max_epochs = config.max_epochs
    mode = config.mode
    if mode == "train":
        print("\nINFO: ML training\n")
        # Used to save dev set predictions.
        # prediction_file = os.path.join(model_path, "temp.predicted")
        # best_val_cost = float("inf")
        first_start = time.time()
        epoch = 0
        while epoch < max_epochs:
            print("\nEpoch:{0}\n".format(epoch))
            start = time.time()
            total_loss = []
            for step, loss in run_train_epoch(model, train_dataloader):
                if math.isnan(loss):
                    print("nan loss")

                if loss:
                    total_loss.append(loss)

                if total_loss:
                    mean_loss = np.mean(total_loss)

                else:
                    mean_loss = float("-inf")

                print(
                    "\rBatch:{0} | Loss:{1} | Mean Loss:{2}\n".format(
                        step, loss, mean_loss
                    )
                )
            # print("\nValidation:\n")
            # run_predict(model, dev_dataloader, prediction_file)
            # val_cost = evaluator(prediction_file)

            # print("\nValidation cost:{0}\n".format(val_cost))
            # if val_cost < best_val_cost:
            #    best_val_cost = val_cost
            #    model.save("best")
            if save_always:
                model.save(str(epoch))
            msg = "\nEpoch training time:{} seconds\n".format(time.time() - start)
            print(msg)
            epoch += 1

        save_config(config, model_path)
        msg = "\nTotal training time:{} seconds\n".format(time.time() - first_start)
        print(msg)
        # Remove the temp output file
        # os.remove(prediction_file)

    elif mode == "test":
        print("Predicting...")
        start = time.time()
        run_predict(model, test_dataloader, config.prediction_file)
        msg = "\nTotal prediction time:{} seconds\n".format(time.time() - start)
        print(msg)


def create_squad_dataset(tokenizer, batch_size, source_max_length, decoder_max_length):
    """Function to create the squad dataset."""
    train_contexts, train_answers = read_squad("./squad/train-v2.0.json")
    val_contexts, val_answers = read_squad("./squad/dev-v2.0.json")

    val_encodings = tokenizer(
        val_contexts,
        truncation=True,
        padding="max_length",
        max_length=source_max_length,
        add_special_tokens=False,
    )
    val_answer_encodings = tokenizer(
        val_answers,
        truncation=True,
        padding="max_length",
        max_length=decoder_max_length,
        add_special_tokens=False,
    )

    train_encodings = tokenizer(
        train_contexts,
        truncation=True,
        padding="max_length",
        max_length=source_max_length,
        add_special_tokens=False,
    )
    train_answer_encodings = tokenizer(
        train_answers,
        truncation=True,
        padding="max_length",
        max_length=decoder_max_length,
        add_special_tokens=False,
    )

    train_encodings["target_ids"] = train_answer_encodings.input_ids
    train_encodings["target_attention_mask"] = train_answer_encodings.attention_mask

    train_encodings["labels"] = train_answer_encodings.input_ids.copy()

    # because BERT automatically shifts the labels, the labels correspond exactly to `target_ids`.
    # We have to make sure that the PAD token is ignored

    train_labels = [
        [-100 if token == tokenizer.pad_token_id else token for token in labels]
        for labels in train_encodings["labels"]
    ]
    train_encodings["labels"] = train_labels

    val_encodings["target_ids"] = val_answer_encodings.input_ids
    val_encodings["target_attention_mask"] = val_answer_encodings.attention_mask

    val_encodings["labels"] = val_answer_encodings.input_ids.copy()

    # because BERT automatically shifts the labels, the labels correspond exactly to `target_ids`.
    # We have to make sure that the PAD token is ignored.

    val_labels = [
        [-100 if token == tokenizer.pad_token_id else token for token in labels]
        for labels in val_encodings["labels"]
    ]
    val_encodings["labels"] = val_labels

    class SquadDataset(torch.utils.data.Dataset):
        def __init__(self, encodings):
            self.encodings = encodings

        def __getitem__(self, idx):
            return {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}

        def __len__(self):
            return len(self.encodings.input_ids)

    train_dataset = SquadDataset(train_encodings)
    val_dataset = SquadDataset(val_encodings)

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    return train_dataset, val_loader, val_loader


def create_rev_squad_dataset(
    tokenizer, batch_size, source_max_length, decoder_max_length
):
    """Function to create the squad dataset."""
    train_contexts, train_answers = read_reverse_squad("./squad/train-v2.0.json")
    val_contexts, val_answers = read_reverse_squad("./squad/dev-v2.0.json")

    val_encodings = tokenizer(
        val_contexts,
        truncation=True,
        padding="max_length",
        max_length=source_max_length,
        add_special_tokens=False,
    )
    val_answer_encodings = tokenizer(
        val_answers,
        truncation=True,
        padding="max_length",
        max_length=decoder_max_length,
        add_special_tokens=False,
    )

    train_encodings = tokenizer(
        train_contexts,
        truncation=True,
        padding="max_length",
        max_length=source_max_length,
        add_special_tokens=False,
    )
    train_answer_encodings = tokenizer(
        train_answers,
        truncation=True,
        padding="max_length",
        max_length=decoder_max_length,
        add_special_tokens=False,
    )

    train_encodings["question_input_ids"] = train_encodings.pop("input_ids")
    train_encodings["question_attention_mask"] = train_encodings.pop("attention_mask")
    train_encodings["question_target_ids"] = train_answer_encodings.input_ids
    train_encodings[
        "question_target_attention_mask"
    ] = train_answer_encodings.attention_mask

    train_encodings["question_labels"] = train_answer_encodings.input_ids.copy()

    # because BERT automatically shifts the labels, the labels correspond exactly to `target_ids`.
    # We have to make sure that the PAD token is ignored

    train_labels = [
        [-100 if token == tokenizer.pad_token_id else token for token in labels]
        for labels in train_encodings["question_labels"]
    ]
    train_encodings["question_labels"] = train_labels

    val_encodings["question_input_ids"] = val_encodings.pop("input_ids")
    val_encodings["question_attention_mask"] = val_encodings.pop("attention_mask")
    val_encodings["question_target_ids"] = val_answer_encodings.input_ids
    val_encodings[
        "question_target_attention_mask"
    ] = val_answer_encodings.attention_mask

    val_encodings["question_labels"] = val_answer_encodings.input_ids.copy()

    # because BERT automatically shifts the labels, the labels correspond exactly to `target_ids`.
    # We have to make sure that the PAD token is ignored.

    val_labels = [
        [-100 if token == tokenizer.pad_token_id else token for token in labels]
        for labels in val_encodings["question_labels"]
    ]
    val_encodings["question_labels"] = val_labels

    class SquadDataset(torch.utils.data.Dataset):
        def __init__(self, encodings):
            self.encodings = encodings

        def __getitem__(self, idx):
            return {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}

        def __len__(self):
            return len(self.encodings.input_ids)

    train_dataset = SquadDataset(train_encodings)
    val_dataset = SquadDataset(val_encodings)

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    return train_dataset, val_loader, val_loader


def run_all(args):
    """Run the T5 on squad, race and narrative qa."""
    config = HyperParameters(
        model_path=args.model_path,
        batch_size=args.batch_size,
        source_max_length=512,
        decoder_max_length=128,
        gpu=args.gpu,
        gpu_device=args.gpu_device,
        learning_rate=args.learning_rate,
        max_epochs=args.max_epochs,
        mode="train",
        num_train_steps=args.num_train_steps,
        prediction_file=args.prediction_file,
    )
    model = T5QA(config)

    nar_train_dataset = create_narrative_dataset(
        tokenizer=model.tokenizer,
        batch_size=config.batch_size,
        source_max_length=config.source_max_length,
        decoder_max_length=config.decoder_max_length,
    )

    race_train_dataset = create_race_dataset(
        tokenizer=model.tokenizer,
        batch_size=config.batch_size,
        source_max_length=config.source_max_length,
        decoder_max_length=config.decoder_max_length,
    )

    sq_train_dataset = create_squad_dataset(
        tokenizer=model.tokenizer,
        batch_size=config.batch_size,
        source_max_length=config.source_max_length,
        decoder_max_length=config.decoder_max_length,
    )

    concat_dataset = torch.utils.data.ConcatDataset(
        [nar_train_dataset, race_train_dataset, sq_train_dataset]
    )
    train_loader = torch.utils.data.DataLoader(
        concat_dataset, batch_size=config.batch_size, shuffle=True
    )
    run_model(
        model,
        config=config,
        evaluator=compute_rouge,
        train_dataloader=train_loader,
        dev_dataloader=None,
        test_dataloader=None,
        save_always=True,
    )


def run_main(args):
    """Decides what to do in the code."""
    if args.mode in ["reqa_train", "reqa_test"]:
        run_reqa(args)


def argument_parser():
    """augments arguments for protein-gene model."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        help="reqa_train | reqa_test",
    )
    parser.add_argument(
        "--model_path",
        type=str,
        required=True,
        help="Path for saving or loading models.",
    )

    # Train specific
    parser.add_argument("--train", type=str, help="file for train data.")

    parser.add_argument("--dev", type=str, help="file for validation data.")

    # Test specific
    parser.add_argument("--test", type=str, help="file for test data.")

    parser.add_argument(
        "--prediction_file", type=str, help="file for saving predictions"
    )

    parser.add_argument("--input_file_name", type=str, help="input file name")

    # Hyper-Parameters
    parser.add_argument("--dim_model", type=int, default=100, help="dim of model units")

    parser.add_argument(
        "--dropout", type=float, default=0.1, help="the probability of zeroing a link"
    )

    parser.add_argument("--dim_embedding", type=int, default=100, help="embedding size")

    parser.add_argument("--learning_rate", type=float, default=0.0005)

    parser.add_argument(
        "--max_gradient_norm",
        type=float,
        default=10.0,
        help="max norm allowed for gradients",
    )

    parser.add_argument("--batch_size", type=int, default=8, help="static batch size")

    parser.add_argument(
        "--num_train_steps", type=int, default=50000, help="number of train steps"
    )

    parser.add_argument(
        "--max_epochs", type=int, default=25, help="max number of training iterations"
    )

    parser.add_argument("--gpu_device", type=int, default=0, help="gpu device to use")

    parser.add_argument("--seed", type=int, default=len("dream"), help="random seed")

    parser.add_argument(
        "--config_file", type=str, default="config.ini", help="config.ini file"
    )

    # GPU or CPU
    parser.add_argument(
        "--gpu", type=bool, default=True, help="on gpu or not? True or False"
    )

    parser.add_argument(
        "--dream_path", type=str, help="path for reading dream scape data!"
    )
    parser.add_argument(
        "--answer_checkpoint", type=str, help="checkpoint of the trained answer model."
    )
    parser.add_argument(
        "--question_checkpoint",
        type=str,
        help="checkpoint of the trained question model.",
    )
    args, _ = parser.parse_known_args()
    return args


if __name__ == "__main__":
    args = argument_parser()
    run_main(args)
