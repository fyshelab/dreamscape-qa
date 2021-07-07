"""Implementation of the T5 Model for Response Generation.

Some parts are from huggingface Library.
https://github.com/huggingface/transformers/tree/master/src/transformers/models/t5
https://arxiv.org/pdf/1910.10683.pdf
https://arxiv.org/abs/1804.04235
"""

import gc
import math
import os
import random
from dataclasses import dataclass
from typing import Any, Optional

import numpy
import torch
from transformers import Adafactor, T5ForConditionalGeneration, T5Tokenizer


@dataclass
class HyperParameters:
    """General Model configuration."""

    model_path: Optional[str] = None
    batch_size: int = 64
    source_max_length: int = 512
    decoder_max_length: int = 128
    config_file: str = "config.ini"
    dim_embedding: int = 100
    dim_model: int = 128
    dropout: float = 0.5
    gpu: bool = False
    l2_norm_weight: float = 0.01
    learning_rate: float = 0.0005
    max_epochs: int = 16
    mode: str = "train"
    train: Optional[str] = None
    prediction_file: Optional[str] = None
    seed: int = 8
    test: Optional[str] = None
    dev: Optional[str] = None
    answer_checkpoint: Optional[str] = "_3_model"
    question_checkpoint: Optional[str] = "_3_model"

    # Related to beam search decoding.
    beam_decoding: Optional[bool] = False
    num_beams: Optional[int] = 5
    no_repeat_ngram_size: Optional[int] = 2
    early_stopping: Optional[bool] = True
    question_training_steps: Optional[int] = 5
    answer_training_steps: Optional[int] = 1


def tuple_of_tensors_to_tensor(tuple_of_tensors):
    return torch.stack(list(tuple_of_tensors), dim=0)


def set_random_seed(seed: int) -> Any:
    """Set the random seed, which initializes the random number generator.

    Ensures that runs are reproducible and eliminates differences due to
    randomness.
    """
    random.seed(seed)
    numpy.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def remove_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix):]
    return text  # or whatever


def torch_save(model: torch.nn.Module, path: str) -> None:
    """Save the model to task at the specified path."""
    torch.save(model.state_dict(), path)


def save(model, model_path: str, checkpoint_name: str):
    """Save the model to the specified path name."""
    torch_save(model, model_path + "_" + checkpoint_name)


def load_module(model, model_path, checkpoint_name):
    # Load the model from the checkpoint.
    loaded_weights = torch.load(
        model_path + checkpoint_name,
        map_location=lambda storage, loc: storage,
    )
    new_weights = {}
    for key, val in loaded_weights.items():
        new_weights[remove_prefix(key, 'module.')] = val
    model.load_state_dict(new_weights)


def clear_cache():
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


def prob_of_sampled_predictions(loss_fct, sample_outputs):
    """Helper function to compute the predictions and the probability of a
    sampled sequence from the output of the generate function in the
    transformers library."""
    # Skip the first pad token generated by the T5 model.
    sampled_predictions = sample_outputs.sequences[:, 1:]
    sampled_scores = sample_outputs.scores
    sampled_scores = tuple_of_tensors_to_tensor(sampled_scores)
    l, n, v = sampled_scores.size()
    log_p = -loss_fct(
        sampled_scores.view(-1, v),
        torch.reshape(torch.transpose(sampled_predictions, 0, 1), (l * n,)),
    ).view(l, n)
    pad_mask = torch.transpose(sampled_predictions, 0, 1) == 0
    good_log_p = log_p.masked_fill_(pad_mask, 0.0)
    log_p = torch.sum(good_log_p, dim=0).squeeze()
    sampled_p = torch.exp(log_p)
    return sampled_predictions, sampled_p


MODEL_NAME = "t5-base"
# MODEL_NAME = "allenai/unifiedqa-t5-base"
Q_MODEL_NAME = "iarfmoose/t5-base-question-generator"


class REQA(torch.nn.Module):
    """Wrapper class around the T5 Model."""

    def __init__(self, cfg: HyperParameters, load_answer=True):
        super(REQA, self).__init__()
        self.config = cfg

        set_random_seed(cfg.seed)

        # Answer model
        answer_tokenizer = T5Tokenizer.from_pretrained(MODEL_NAME)

        # Construct the answer model
        answer_model = T5ForConditionalGeneration.from_pretrained(MODEL_NAME)

        # Question Model
        question_tokenizer = T5Tokenizer.from_pretrained(Q_MODEL_NAME)

        # Construct the question model
        question_model = T5ForConditionalGeneration.from_pretrained(Q_MODEL_NAME)

        if cfg.mode == "train":
            # Configurations suggested by the T5 paper.
            self.answer_optimizer = Adafactor(
                answer_model.parameters(),
                lr=cfg.learning_rate,
                eps=(1e-30, 1e-3),
                clip_threshold=1.0,
                decay_rate=-0.8,
                beta1=None,
                weight_decay=0.0,
                relative_step=False,
                scale_parameter=False,
                warmup_init=False,
            )

            # Configurations suggested by the T5 paper.
            self.question_optimizer = Adafactor(
                question_model.parameters(),
                lr=cfg.learning_rate,
                eps=(1e-30, 1e-3),
                clip_threshold=1.0,
                decay_rate=-0.8,
                beta1=None,
                weight_decay=0.0,
                relative_step=False,
                scale_parameter=False,
                warmup_init=False,
            )

            if not os.path.exists(cfg.model_path):
                os.makedirs(cfg.model_path)
            self.model_path = os.path.join(cfg.model_path, "model")

            if load_answer:
                # we have a pre-trained answer module.
                load_module(answer_model, self.model_path, cfg.answer_checkpoint)

        elif cfg.mode in ["test", "inference"]:
            self.model_path = os.path.join(cfg.model_path, "model")
            load_module(answer_model, self.model_path, cfg.answer_checkpoint)
            load_module(question_model, self.model_path, cfg.question_checkpoint)

        self.answer_model = answer_model
        self.answer_tokenizer = answer_tokenizer
        self.question_model = question_model
        self.question_tokenizer = question_tokenizer

    def question_greedy_predict(self, batch):
        """Greedily generate the questions and prepare inputs for the answer
        module."""
        question_input_ids = batch["entity_relation_passage_input_ids"]
        question_input_mask = batch["entity_relation_passage_attention_mask"]
        if self.config.gpu:
            question_input_ids = question_input_ids.cuda()
            question_input_mask = question_input_mask.cuda()

        question_predictions = self.question_model.generate(
            input_ids=question_input_ids,
            attention_mask=question_input_mask,
        )

        question_predictions_str = self.question_tokenizer.batch_decode(
            question_predictions, skip_special_tokens=True
        )

        new_articles = []
        for i in range(len(batch["passages"])):
            new_article = (
                "question: "
                + question_predictions_str[i]
                + " context: "
                + batch["passages"][i]
                + " </s>"
            )
            new_articles.append(new_article)

        answer_inputs = self.answer_tokenizer(
            new_articles,
            truncation=True,
            padding="max_length",
            max_length=self.config.source_max_length,
            add_special_tokens=False,
            return_tensors="pt",
        )
        answer_input_ids = answer_inputs.input_ids
        answer_input_mask = answer_inputs.attention_mask
        if self.config.gpu:
            answer_input_ids = answer_input_ids.cuda()
            answer_input_mask = answer_input_mask.cuda()

        return answer_input_ids, answer_input_mask

    def predict(self, batch):
        # Free memory in GPU, very important!
        clear_cache()
        # disable dropout
        self.answer_model.eval()
        self.question_model.eval()

        answer_input_ids, answer_input_mask = self.question_greedy_predict(batch)

        second_entity_predictions = self.answer_model.generate(
            input_ids=answer_input_ids,
            attention_mask=answer_input_mask,
        )
        second_entity_predictions_str = self.answer_tokenizer.batch_decode(
            second_entity_predictions, skip_special_tokens=True
        )
        for index in range(len(second_entity_predictions_str)):
            pred_str = second_entity_predictions_str[index]
            output_batch = {
                "predictions_str": pred_str,
            }
            yield output_batch

    def train(self, batch, phase="answer", answer_lambda=0.1, question_lambda=0.1):
        # Free memory in GPU, very important!
        clear_cache()
        # Turn on training mode which enables dropout.
        if phase == "answer":
            self.question_model.eval()
            self.question_optimizer.zero_grad()

            answer_input_ids, answer_input_mask = self.question_greedy_predict(batch)

            self.answer_model.train()
            self.answer_optimizer.zero_grad()

            target_mask = batch["second_entity_attention_mask"]
            labels = batch["second_entity_labels"]
            if self.config.gpu:
                target_mask = target_mask.cuda()
                labels = labels.cuda()

            output = self.answer_model(
                input_ids=answer_input_ids,
                attention_mask=answer_input_mask,
                decoder_attention_mask=target_mask,
                labels=labels,
            )

            re_loss = output.loss

            clear_cache()

            # Loss from the correct QA examples!
            input_ids = batch["input_ids"]
            input_mask = batch["attention_mask"]
            target_mask = batch["target_attention_mask"]
            labels = batch["labels"]
            if self.config.gpu:
                input_ids = input_ids.cuda()
                input_mask = input_mask.cuda()
                target_mask = target_mask.cuda()
                labels = labels.cuda()

            output = self.answer_model(
                input_ids=input_ids,
                attention_mask=input_mask,
                decoder_attention_mask=target_mask,
                labels=labels,
            )

            qa_loss = output.loss

            loss = answer_lambda * qa_loss + re_loss
            loss_value = loss.item()

            # BackProp
            loss.backward()

            # Optimize
            self.answer_optimizer.step()

            return {"loss_value": loss_value}

        elif phase == "question":
            self.question_optimizer.zero_grad()
            self.answer_optimizer.zero_grad()
            self.question_model.train()
            self.answer_model.eval()

            loss_fct = torch.nn.CrossEntropyLoss(ignore_index=-100, reduction="none")
            if self.config.gpu:
                loss_fct = loss_fct.cuda()

            # Loss from the entity relation examples!
            question_input_ids = batch["entity_relation_passage_input_ids"]
            question_input_mask = batch["entity_relation_passage_attention_mask"]
            if self.config.gpu:
                question_input_ids = question_input_ids.to(self.device)
                question_input_mask = question_input_mask.to(self.device)

            b_sz, _ = question_input_ids.size()

            # Use top-p sampling to collect random samples.
            sampled_question_outputs = self.question_model.generate(
                input_ids=question_input_ids,
                attention_mask=question_input_mask,
                do_sample=True,
                no_repeat_ngram_size=self.config.no_repeat_ngram_size,
                early_stopping=self.config.early_stopping,
                max_length=self.config.decoder_max_length,
                num_return_sequences=self.config.num_beams // 2,
                top_p=0.95,
                output_scores=True,
                return_dict_in_generate=True,
            )

            top_p_questions, sampled_p = prob_of_sampled_predictions(
                loss_fct, sampled_question_outputs
            )

            # Use beam search to collect samples.
            beam_question_outputs = self.question_model.generate(
                input_ids=question_input_ids,
                attention_mask=question_input_mask,
                no_repeat_ngram_size=self.config.no_repeat_ngram_size,
                early_stopping=self.config.early_stopping,
                max_length=self.config.decoder_max_length,
                num_return_sequences=self.config.num_beams // 2,
                num_beams=self.config.num_beams // 2,
                output_scores=True,
                return_dict_in_generate=True,
            )

            beam_questions, beam_p = prob_of_sampled_predictions(
                loss_fct, beam_question_outputs
            )

            p = torch.cat(
                (
                    sampled_p.view(self.config.num_beams // 2, b_sz),
                    beam_p.view(self.config.num_beams // 2, b_sz),
                ),
                dim=0,
            )
            re_p_question = p.view(self.config.num_beams * b_sz).view(
                self.config.num_beams, b_sz
            )

            sampled_question_predictions_str = self.question_tokenizer.batch_decode(
                top_p_questions, skip_special_tokens=True
            )

            sampled_question_predictions_str_reshaped = [
                sampled_question_predictions_str[
                    i
                    * (self.config.num_beams // 2) : (i + 1)
                    * (self.config.num_beams // 2)
                ]
                for i in range(b_sz)
            ]

            beam_question_predictions_str = self.question_tokenizer.batch_decode(
                beam_questions, skip_special_tokens=True
            )

            beam_question_predictions_str_reshaped = [
                beam_question_predictions_str[
                    i
                    * (self.config.num_beams // 2) : (i + 1)
                    * (self.config.num_beams // 2)
                ]
                for i in range(b_sz)
            ]

            new_articles = []
            for i in range(b_sz):
                for j in range(self.config.num_beams // 2):
                    new_article = (
                        "question: "
                        + sampled_question_predictions_str_reshaped[i][j]
                        + " context: "
                        + batch["passages"][i]
                        + " </s>"
                    )
                    new_articles.append(new_article)
                for j in range(self.config.num_beams // 2):
                    new_article = (
                        "question: "
                        + beam_question_predictions_str_reshaped[i][j]
                        + " context: "
                        + batch["passages"][i]
                        + " </s>"
                    )
                    new_articles.append(new_article)

            answer_inputs = self.answer_tokenizer(
                new_articles,
                truncation=True,
                padding="max_length",
                max_length=self.config.source_max_length,
                add_special_tokens=False,
                return_tensors="pt",
            )

            answer_input_ids = answer_inputs.input_ids
            answer_input_mask = answer_inputs.attention_mask
            if self.config.gpu:
                answer_input_ids = answer_input_ids.cuda()
                answer_input_mask = answer_input_mask.cuda()

            target_mask = batch["second_entity_attention_mask"]
            labels = batch["second_entity_labels"]
            if self.config.gpu:
                target_mask = target_mask.cuda()
                labels = labels.cuda()

            b_sz, seq_len = labels.size()
            labels = labels.repeat(1, self.config.num_beams).view(-1, seq_len)
            target_mask = target_mask.repeat(1, self.config.num_beams).view(-1, seq_len)

            output = self.answer_model(
                input_ids=answer_input_ids,
                attention_mask=answer_input_mask,
                decoder_attention_mask=target_mask,
                decoder_input_ids=self.answer_model._shift_right(labels),
                labels=None,
            )

            log_p = -loss_fct(
                output.logits.view(-1, output.logits.size(-1)),
                labels.view(-1),
            )

            b, sz, v = output.logits.size()
            log_p = log_p.view(b, sz)
            good_log_p = log_p.masked_fill_(labels == -100, 0.0)
            log_p = torch.sum(good_log_p, dim=1).squeeze()
            p = torch.exp(log_p)
            re_p_answer = p.view(self.config.num_beams, b_sz)
            re_loss = -torch.mean(
                torch.log(
                    torch.sum(
                        torch.mul(
                            torch.transpose(re_p_answer, 0, 1),
                            torch.transpose(re_p_question, 0, 1),
                        ),
                        dim=1,
                    )
                ),
                dim=0,
            )

            clear_cache()

            # Loss from the correct QA examples!
            input_ids = batch["question_input_ids"]
            input_mask = batch["question_attention_mask"]
            target_mask = batch["question_target_attention_mask"]
            labels = batch["question_labels"]
            if self.config.gpu:
                input_ids = input_ids.cuda()
                input_mask = input_mask.cuda()
                target_mask = target_mask.cuda()
                labels = labels.cuda()

            output = self.question_model(
                input_ids=input_ids,
                attention_mask=input_mask,
                decoder_attention_mask=target_mask,
                labels=labels,
            )

            qa_loss = output.loss

            loss = question_lambda * qa_loss + re_loss
            loss_value = loss.item()

            # BackProp
            loss.backward()

            # Optimize
            self.question_optimizer.step()

            return {"loss_value": loss_value}
