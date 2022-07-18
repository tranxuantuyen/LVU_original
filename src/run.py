import argparse
import glob
import logging
import os
import pickle
import random
import re
import shutil
from typing import Dict, List, Tuple
import time

import numpy as np
import torch
import torch.nn as nn
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader, Dataset, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange
import wandb
from models import (
    WEIGHTS_NAME,
    AdamW,
    PreTrainedModel,
    RobertaConfig,
    RobertaForMaskedLM,
    get_linear_schedule_with_warmup,
)
from data import video_data_helper
from data.video_data_helper import binarize
from utils.ava_eval_helper import evaluate_ava


logger = logging.getLogger(__name__)


MODEL_CLASSES = {
    "roberta": (RobertaConfig, RobertaForMaskedLM),
}

ZERO_FEAT2304 = np.zeros((2304,))

EVAL_START_SEC = 902  # inclusive
EVAL_END_SEC = 1799  # not inclusive


with open(
    "/home/s222126678/Documents/lvu_trans/data/ava/slowfast_baseline_outputs/ava_eval_data.pkl",
    "rb",
) as f:
    (
        excluded_keys,
        class_whitelist,
        categories,
        groundtruth,
        video_idx_to_name,
    ) = pickle.load(f)
with open(
    "/home/s222126678/Documents/lvu_trans/data/ava/slowfast_baseline_outputs/predictions-29.4.pkl",
    "rb",
) as f:
    (all_preds, all_ori_boxes, all_metadata) = pickle.load(f)
video_name_to_idx = {
    video_idx_to_name[key]: key for key in range(len(video_idx_to_name))
}
logger.info(video_name_to_idx)
logger.info(video_idx_to_name)

proj_W = None
proj_b = None


class VideoDataset(Dataset):
    def __init__(self, args, evaluate):

        self.evaluate = evaluate
        self.secs_per_example = args.secs_per_example

        self.all_features = video_data_helper.load_features(
            args.eval_feature_file if evaluate else args.train_feature_file,
            args,
        )
        self.videos = video_data_helper.load_video_data(
            args.eval_data_file if evaluate else args.train_data_file,
            args,
        )
        self.args = args
        self.spans = []
        for video_name in self.videos.keys():
            v = self.videos[video_name]
            # for action recognition only, both train and test use 15 min only.
            # gap = args.secs_per_example - 2 if evaluate else 1

            for center_sec in range(EVAL_START_SEC, EVAL_END_SEC):
                if (
                    sum(
                        [
                            sec in v.keys()
                            for sec in range(
                                center_sec - self.secs_per_example // 2,
                                center_sec + self.secs_per_example // 2,
                            )
                        ]
                    )
                    > 0
                ):
                    self.spans.append((video_name, center_sec))
        # if evaluate:
        #     self.spans = self.spans * args.eval_sample_x

        print(len(set([x[0] for x in self.spans])), "videos in spans in total")
        print(len(self.videos), "video data loaded in total")

    def __len__(self):
        return len(self.spans)

    def __getitem__(self, item):
        selected = [self.spans[item]]

        ret = []
        construct_func = self.construct_example

        for (video_name, center_start) in selected:
            for _ in range(100):
                one_ex = construct_func(video_name, center_start=center_start)
                if one_ex is not None:
                    break
            ret.append(one_ex + [video_name])
        return ret

    def construct_example(self, video_name, center_start=None):
        def get_spatial_encoding(box, perturb=0.0):
            box = [float(x) for x in box.split(",")]
            if perturb > 0 and not self.evaluate:
                p0 = (box[2] - box[0]) * perturb
                p1 = (box[3] - box[1]) * perturb
                box = [
                    box[0] + p0 * random.uniform(-1.0, 1.0),
                    box[1] + p1 * random.uniform(-1.0, 1.0),
                    box[2] + p0 * random.uniform(-1.0, 1.0),
                    box[3] + p1 * random.uniform(-1.0, 1.0),
                ]
            box.append((box[2] - box[0]) * (box[3] - box[1]))
            return np.array(box)

        args = self.args

        video = self.videos[video_name]

        video_features = (
            self.all_features[video_name] if (self.all_features is not None) else None
        )

        ex_link_ids = []
        ex_scene_ids = []
        ex_boxes = []
        ex_secs = []
        ex_actions = []
        ex_long_term = []
        ex_features = []
        ex_spatial = []

        for shift_idx, sec_shift in enumerate(range(self.secs_per_example)):

            if center_start is not None:
                if sec_shift % 2 == 0:
                    sec = center_start + (sec_shift + 1) // 2
                    auged_sec = center_start + (shift_idx + 1) // 2
                else:
                    sec = center_start - (sec_shift + 1) // 2
                    auged_sec = center_start - (shift_idx + 1) // 2
            if sec in video:
                for box, (scene_id, link_id, actions) in video[sec].items():

                    if len(ex_link_ids) < args.max_position_embeddings - 4:
                        ex_link_ids.append(link_id)
                        ex_secs.append(auged_sec)
                        ex_scene_ids.append(scene_id)
                        ex_boxes.append(box)
                        ex_actions.append(binarize(actions))

                        cur_feat = video_features[sec][box]

                        ex_features.append(cur_feat)

                        ex_spatial.append(get_spatial_encoding(box, 0.2))

        if len(ex_secs) == 0:
            return None

        assert (max(ex_secs) - min(ex_secs)) < args.secs_per_example

        halfway = args.secs_per_example // 2

        increasing_pos_ids = [x - min(ex_secs) for x in ex_secs]
        decreasing_pos_ids = [max(ex_secs) - x for x in ex_secs]
        center_pos_ids = [max(0, x - center_start + halfway) for x in ex_secs]

        increasing_scene_ids = [x - min(ex_scene_ids) for x in ex_scene_ids]
        decreasing_scene_ids = [max(ex_scene_ids) - x for x in ex_scene_ids]

        dists = [abs(x - center_start) for x in ex_secs]
        for dist, tmp_scene_id in zip(dists, ex_scene_ids):
            if dist == min(dists):
                center_scene_id = tmp_scene_id

        center_scene_ids = [max(0, x - center_scene_id + halfway) for x in ex_scene_ids]

        n_links = len(set(ex_link_ids))
        rand_link_ids = dict(
            zip(
                list(set(ex_link_ids)),
                random.sample(range(n_links), n_links),
            )
        )
        ex_link_ids = [rand_link_ids[x] + 2 for x in ex_link_ids]

        ex_actions = [binarize([])] + ex_actions + [binarize([])]

        ex_long_term = []

        ex_link_ids = [0] + ex_link_ids + [1]  # end doens't belong to a link

        increasing_pos_ids = (
            [0] + [x + 2 for x in increasing_pos_ids] + [1]
        )  # end can have a new pos
        decreasing_pos_ids = (
            [0] + [x + 2 for x in decreasing_pos_ids] + [1]
        )  # end can have a new pos
        center_pos_ids = (
            [0] + [x + 2 for x in center_pos_ids] + [1]
        )  # end can have a new pos

        increasing_scene_ids = [0] + [x + 2 for x in increasing_scene_ids] + [1]
        decreasing_scene_ids = [0] + [x + 2 for x in decreasing_scene_ids] + [1]
        center_scene_ids = [0] + [x + 2 for x in center_scene_ids] + [1]

        ex_features = [ZERO_FEAT2304] + ex_features + [ZERO_FEAT2304]

        ex_spatial = [ex_spatial[0] * 0.0] + ex_spatial + [ex_spatial[0] * 0.0]

        return [
            torch.tensor(ex_link_ids) + 2,
            torch.tensor(increasing_pos_ids) + 2,
            torch.tensor(decreasing_pos_ids) + 2,
            torch.tensor(center_pos_ids) + 2,
            torch.tensor(increasing_scene_ids) + 2,
            torch.tensor(decreasing_scene_ids) + 2,
            torch.tensor(center_scene_ids) + 2,
            torch.tensor(ex_actions),
            torch.tensor(ex_long_term),
            torch.from_numpy(np.ascontiguousarray(ex_features)),
            torch.tensor(ex_spatial),
            ex_secs,
            ex_boxes,
        ]


def set_seed(args):
    seed = args.seed + args.local_rank + 1
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(seed)


def shared_collate(all_examples: List[torch.Tensor]):
    if len(all_examples[0]) == 1:
        all_examples = [x[0] for x in all_examples]
    elif len(all_examples[0]) == 2:
        all_examples = [x[0] for x in all_examples] + [x[1] for x in all_examples]

    assert len(all_examples[0]) == 14
    zipped = list(zip(*all_examples))

    meta = [list(examples) for examples in zipped[9:]]

    padding_value = 1
    padding_values = [padding_value] * 7 + [-100] * 2

    return [
        pad_sequence(list(examples), batch_first=True, padding_value=padding_values[i])
        for i, examples in enumerate(zipped[:9])
    ] + meta


def prepare_model_input(
    link_batch,
    inc_pos_batch,
    dec_pos_batch,
    center_pos_batch,
    inc_scene_batch,
    dec_scene_batch,
    center_scene_batch,
    action_batch,
    feature_batch,
    spatial_batch,
    sec_batch,
    args,
    is_eval=False,
):

    inputs_embed_batch = pad_feature_batch(feature_batch, args.device)

    spatial_batch = pad_feature_batch(spatial_batch, args.device)

    outputs_embed_batch = inputs_embed_batch.clone().detach()

    if action_batch is not None:
        action_batch = action_batch.to(args.device)

    target_locations = None

    link_batch = link_batch.to(args.device)

    inc_pos_batch = inc_pos_batch.to(args.device)
    dec_pos_batch = dec_pos_batch.to(args.device)
    center_pos_batch = center_pos_batch.to(args.device)

    inc_scene_batch = inc_scene_batch.to(args.device)
    dec_scene_batch = dec_scene_batch.to(args.device)
    center_scene_batch = center_scene_batch.to(args.device)

    return (
        action_batch,
        link_batch,
        inc_pos_batch,
        dec_pos_batch,
        center_pos_batch,
        inc_scene_batch,
        dec_scene_batch,
        center_scene_batch,
        inputs_embed_batch,
        outputs_embed_batch,
        spatial_batch,
        target_locations,
    )

def pad_feature_batch(feature_batch, device):
    batch_size = len(feature_batch)
    max_len = max([len(x) for x in feature_batch])
    dim = feature_batch[0][0].shape[0]

    batch = torch.zeros((batch_size, max_len, dim), device=device)
    for i in range(batch_size):
        batch[i, : len(feature_batch[i])] = feature_batch[i].to(device)
    return batch


def train(args, train_dataset, model: PreTrainedModel) -> Tuple[int, float]:
    """Train the model"""

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)

    train_sampler = (
        RandomSampler(train_dataset)
        if args.local_rank == -1
        else DistributedSampler(train_dataset)
    )

    def collate(all_examples: List[torch.Tensor]):
        return shared_collate(all_examples)

    train_dataloader = DataLoader(
        train_dataset,
        sampler=train_sampler,
        batch_size=args.train_batch_size,
        collate_fn=collate,
        num_workers=args.num_workers,
        pin_memory=True,
    )

    t_total = len(train_dataloader) // args.gradient_accumulation_steps
    no_decay = ["bias", "LayerNorm.weight"]
    model.init_weights()
    rbt_no_d = []
    final_no_d = []
    rbt_d = []
    final_d = []
    for n, p in model.named_parameters():
        if any(nd in n for nd in no_decay):
            if "roberta" in n:
                rbt_no_d.append(p)
            else:
                final_no_d.append(p)
        else:
            if "roberta" in n:
                rbt_d.append(p)
            else:
                final_d.append(p)

    optimizer_grouped_parameters = [
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if not any(nd in n for nd in no_decay)
            ],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if any(nd in n for nd in no_decay)
            ],
            "weight_decay": 0.0,
        },
    ]

    optimizer = AdamW(
        optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon
    )
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=round(t_total * 0.1), num_training_steps=t_total
    )

    # Check if saved optimizer or scheduler states exist

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info(
        "  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size
    )
    logger.info(
        "  Total train batch size (w. parallel, distributed & accumulation) = %d",
        args.train_batch_size
        * args.gradient_accumulation_steps
        * (torch.distributed.get_world_size() if args.local_rank != -1 else 1),
    )
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    epochs_trained = 0
    # Check if continuing training from a checkpoint
    model = model.to(args.device)
    tr_loss = 0.0
    model.zero_grad()
    train_iterator = trange(
        epochs_trained,
        1 if args.is_end_task else int(args.num_train_epochs),
        desc="Epoch",
        disable=args.local_rank not in [-1, 0],
    )
    set_seed(args)  # Added here for reproducibility

    logger.info(model)

    for cur_epoch in train_iterator:
        epoch_iterator = tqdm(
            train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0]
        )

        for step, (
            link_batch,
            inc_pos_batch,
            dec_pos_batch,
            center_pos_batch,
            inc_scene_batch,
            dec_scene_batch,
            center_scene_batch,
            action_batch,
            long_term_batch,
            feature_batch,
            spatial_batch,
            sec_batch,
            box_batch,
            video_name_batch,
        ) in enumerate(epoch_iterator):

            (
                action_batch,
                link_batch,
                inc_pos_batch,
                dec_pos_batch,
                center_pos_batch,
                inc_scene_batch,
                dec_scene_batch,
                center_scene_batch,
                inputs_embed_batch,
                outputs_embed_batch,
                spatial_batch,
                target_locations,
            ) = prepare_model_input(
                link_batch,
                inc_pos_batch,
                dec_pos_batch,
                center_pos_batch,
                inc_scene_batch,
                dec_scene_batch,
                center_scene_batch,
                action_batch,
                feature_batch,
                spatial_batch,
                sec_batch,
                args,
            )

            model.train()

            outputs = model(
                link_ids=None if args.no_link_ids else link_batch,
                inc_scene_ids=None if args.no_scene_ids else inc_scene_batch,
                dec_scene_ids=None if args.no_scene_ids else dec_scene_batch,
                center_scene_ids=None if args.no_scene_ids else center_scene_batch,
                inc_position_ids=None if args.no_pos_ids else inc_pos_batch,
                dec_position_ids=None if args.no_pos_ids else dec_pos_batch,
                center_position_ids=None if args.no_pos_ids else center_pos_batch,
                action_labels=action_batch,  ####
                long_term_labels=long_term_batch,
                inputs_embeds=inputs_embed_batch,
                outputs_embeds=outputs_embed_batch,
                spatial_codes=spatial_batch,
                target_locations=None,
                secs=sec_batch,
                boxes=box_batch,
                args=args,
            )
            losses = outputs[
                0
            ]  # model outputs are always tuple in transformers (see doc)

            if step == 0:
                logger.info(losses)

            loss = sum(losses.values())

            loss.backward()
            if args.do_wandb:
                wandb.log(
                    {
                        "loss_batch": loss,
                    }
                )
            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

        print("done one epoch")

    return global_step, tr_loss / global_step


def evaluate_action_recognition(bert_all_preds, args):

    logger.info("bert output to dict")
    bert_preds = {}
    for pred_batch, video_name_batch, sec_batch, box_batch, is_center in bert_all_preds:
        pred_batch = torch.sigmoid(pred_batch)

        for i in range(len(video_name_batch)):
            video_idx = video_name_to_idx[video_name_batch[i]]

            secs = sec_batch[i]
            boxes = box_batch[i]
            for j, (ex_sec, ex_box) in enumerate(zip(secs, boxes)):

                if not is_center[i, j + 1]:
                    continue
                if video_idx not in bert_preds:
                    bert_preds[video_idx] = {}

                if isinstance(ex_sec, int):
                    sec_list = [ex_sec]
                    box_list = [ex_box]
                else:
                    sec_list = ex_sec
                    box_list = ex_box
                for sec, box in zip(sec_list, box_list):
                    if sec not in bert_preds[video_idx]:
                        bert_preds[video_idx][sec] = {}

                    if box in bert_preds[video_idx][sec]:
                        #### WTF it should be j + 1.
                        bert_preds[video_idx][sec][box].append(pred_batch[i, j + 1])
                    else:
                        bert_preds[video_idx][sec][box] = [pred_batch[i, j + 1]]

    logger.info("set all_preds to bert")
    used_count = 0
    # all_preds[:, :] = 0.0
    for i in range(all_preds.shape[0]):
        video_idx = int(all_metadata[i][0])
        sec = int(all_metadata[i][1])
        box = ",".join(["%.03f" % x for x in all_ori_boxes[i][1:]])
        if (
            video_idx in bert_preds
            and sec in bert_preds[video_idx]
            and box in bert_preds[video_idx][sec]
        ):
            pred_list = bert_preds[video_idx][sec][box]
            all_preds[i, :] = sum(pred_list) / len(pred_list)
            used_count += 1

    logger.info("%d predictions used" % used_count)
    logger.info("%d predictions in total" % all_preds.shape[0])

    mean_ap = evaluate_ava(
        all_preds,
        all_ori_boxes,
        all_metadata.tolist(),
        excluded_keys,
        class_whitelist,
        categories,
        groundtruth=groundtruth,
        video_idx_to_name=video_idx_to_name,
    )
    return mean_ap * 100.0


def softmax(x):
    """Compute softmax values for each sets of scores in x."""
    e_x = np.exp(x - x.max())
    return e_x / e_x.sum()


def evaluate(args, model: PreTrainedModel, prefix="") -> Dict:

    logger.info(model)
    # Loop to handle MNLI double evaluation (matched, mis-matched)
    eval_output_dir = args.output_dir
    eval_dataset = VideoDataset(args, evaluate=True)

    if args.local_rank in [-1, 0]:
        os.makedirs(eval_output_dir, exist_ok=True)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    # Note that DistributedSampler samples randomly

    def collate(all_examples: List[torch.Tensor]):
        return shared_collate(all_examples)

    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(
        eval_dataset,
        sampler=eval_sampler,
        batch_size=args.eval_batch_size,
        collate_fn=collate,
        num_workers=args.num_workers_eval,
        pin_memory=True,
    )

    # multi-gpu evaluate
    if args.n_gpu > 1 and not isinstance(model, torch.nn.DataParallel):
        model = torch.nn.DataParallel(model)

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)
    eval_loss = 0.0
    all_eval_loss = 0.0

    long_term_top1 = 0.0
    long_term_count = 0

    nb_eval_steps = 0
    eval_example_count = 0
    model.eval()

    all_preds = []
    for (
        link_batch,
        inc_pos_batch,
        dec_pos_batch,
        center_pos_batch,
        inc_scene_batch,
        dec_scene_batch,
        center_scene_batch,
        action_batch,
        long_term_batch,
        feature_batch,
        spatial_batch,
        sec_batch,
        box_batch,
        video_name_batch,
    ) in tqdm(eval_dataloader, desc="Evaluating"):

        (
            action_batch,
            link_batch,
            inc_pos_batch,
            dec_pos_batch,
            center_pos_batch,
            inc_scene_batch,
            dec_scene_batch,
            center_scene_batch,
            inputs_embed_batch,
            outputs_embed_batch,
            spatial_batch,
            target_locations,
        ) = prepare_model_input(
            link_batch,
            inc_pos_batch,
            dec_pos_batch,
            center_pos_batch,
            inc_scene_batch,
            dec_scene_batch,
            center_scene_batch,
            action_batch,
            feature_batch,
            spatial_batch,
            sec_batch,
            args,
            is_eval=True,
        )

        with torch.no_grad():
            outputs = model(
                link_ids=None if args.no_link_ids else link_batch,
                inc_scene_ids=None if args.no_scene_ids else inc_scene_batch,
                dec_scene_ids=None if args.no_scene_ids else dec_scene_batch,
                center_scene_ids=None if args.no_scene_ids else center_scene_batch,
                inc_position_ids=None if args.no_pos_ids else inc_pos_batch,
                dec_position_ids=None if args.no_pos_ids else dec_pos_batch,
                center_position_ids=None if args.no_pos_ids else center_pos_batch,
                action_labels=action_batch,
                long_term_labels=long_term_batch,
                inputs_embeds=inputs_embed_batch,
                outputs_embeds=outputs_embed_batch,
                spatial_codes=spatial_batch,
                target_locations=target_locations,
                secs=sec_batch,
                boxes=box_batch,
                args=args,
            )
            losses = outputs[0]
            all_preds.append(
                (
                    outputs[1]["pred"].cpu(),
                    video_name_batch,
                    sec_batch,
                    box_batch,
                    (action_batch[:, :, 0] != -100).cpu(),
                )
            )
            eval_loss += sum([loss.mean() for loss in losses.values()]).item()
            eval_example_count += inc_pos_batch.shape[0]
        nb_eval_steps += 1

    mean_ap = 0.0
    start_eval = time.time()
    mean_ap = evaluate_action_recognition(all_preds, args)
    logger.info("eval done in {} secs".format(time.time() - start_eval))

    clip_mse = []
    split_result = {}
    eval_loss = eval_loss / nb_eval_steps
    all_eval_loss = all_eval_loss / nb_eval_steps
    perplexity = torch.exp(torch.tensor(eval_loss))
    total_perplexity = torch.exp(torch.tensor(all_eval_loss))

    if long_term_count > 0:
        long_term_top1 = float(long_term_top1) / float(long_term_count)

    result = {
        "perplexity": perplexity,
        "all_eval_loss": all_eval_loss,
        "total_perplexity": total_perplexity,
        "map": mean_ap,
        "clip_mse": np.mean(clip_mse),
        "long_term_top1": long_term_top1,
    }
    for split in split_result.keys():
        result["agg_" + split] = split_result[split]

    output_eval_file = os.path.join(eval_output_dir, prefix, "eval_results.txt")
    with open(output_eval_file, "w") as writer:
        logger.info(
            "***** Eval results {} ({} examples) *****".format(
                prefix, eval_example_count
            )
        )
        for key in sorted(result.keys()):
            logger.info("  %s = %s", key, str(result[key]))
            writer.write("%s = %s\n" % (key, str(result[key])))

    return result


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--train_data_file",
        default=None,
        type=str,
        required=True,
        help="The input training data file (a text file).",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--model_type",
        type=str,
        required=True,
        help="The model architecture to be trained or fine-tuned.",
    )

    # Other parameters
    parser.add_argument(
        "--eval_data_file",
        default=None,
        type=str,
        help="An optional input evaluation data file to evaluate the perplexity on (a text file).",
    )
    parser.add_argument(
        "--do_wandb",
        action="store_true",
    )
    parser.add_argument(
        "--line_by_line",
        action="store_true",
        help="Whether distinct lines of text in the dataset are to be handled as distinct sequences.",
    )
    parser.add_argument(
        "--should_continue",
        action="store_true",
        help="Whether to continue from latest checkpoint in output_dir",
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        help="The model checkpoint for weights initialization. Leave None if you want to train a model from scratch.",
    )

    parser.add_argument(
        "--mlm",
        action="store_true",
        help="Train with masked-language modeling loss instead of language modeling.",
    )
    parser.add_argument(
        "--mlm_probability",
        type=float,
        default=0.15,
        help="Ratio of tokens to mask for masked language modeling loss",
    )

    parser.add_argument(
        "--config_name",
        default=None,
        type=str,
        help="Optional pretrained config name or path if not the same as model_name_or_path. If both are None, initialize a new config.",
    )
    parser.add_argument(
        "--cache_dir",
        default=None,
        type=str,
        help="Optional directory to store the pre-trained models downloaded from s3 (instead of the default one)",
    )
    parser.add_argument(
        "--do_train", action="store_true", help="Whether to run training."
    )
    parser.add_argument(
        "--do_eval", action="store_true", help="Whether to run eval on the dev set."
    )
    parser.add_argument(
        "--evaluate_during_training",
        action="store_true",
        help="Run evaluation during training at each logging step.",
    )

    parser.add_argument(
        "--per_gpu_train_batch_size",
        default=4,
        type=int,
        help="Batch size per GPU/CPU for training.",
    )
    parser.add_argument(
        "--per_gpu_eval_batch_size",
        default=4,
        type=int,
        help="Batch size per GPU/CPU for evaluation.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--learning_rate",
        default=5e-5,
        type=float,
        help="The initial learning rate for Adam.",
    )
    parser.add_argument(
        "--weight_decay", default=0.0, type=float, help="Weight decay if we apply some."
    )
    parser.add_argument(
        "--adam_epsilon", default=1e-8, type=float, help="Epsilon for Adam optimizer."
    )
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm."
    )
    parser.add_argument(
        "--num_train_epochs",
        default=10.0,
        type=float,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument(
        "--warmup_steps", default=0, type=int, help="Linear warmup over warmup_steps."
    )

    parser.add_argument(
        "--logging_steps", type=int, default=4000, help="Log every X updates steps."
    )
    parser.add_argument(
        "--save_steps",
        type=int,
        default=4000,
        help="Save checkpoint every X updates steps.",
    )
    parser.add_argument(
        "--save_total_limit",
        type=int,
        default=None,
        help="Limit the total amount of checkpoints, delete the older checkpoints in the output_dir, does not delete by default",
    )
    parser.add_argument(
        "--eval_all_checkpoints",
        action="store_true",
        help="Evaluate all checkpoints starting with the same prefix as model_name_or_path ending and ending with step number",
    )
    parser.add_argument(
        "--no_cuda", action="store_true", help="Avoid using CUDA when available"
    )
    parser.add_argument(
        "--overwrite_output_dir",
        action="store_true",
        help="Overwrite the content of the output directory",
    )
    parser.add_argument(
        "--overwrite_cache",
        action="store_true",
        help="Overwrite the cached training and evaluation sets",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed for initialization"
    )
    parser.add_argument("--max_iter", type=int, default=-1, help="")

    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit",
    )
    parser.add_argument(
        "--fp16_opt_level",
        type=str,
        default="O1",
        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
        "See details at https://nvidia.github.io/apex/amp.html",
    )
    parser.add_argument(
        "--local_rank",
        type=int,
        default=-1,
        help="For distributed training: local_rank",
    )
    parser.add_argument(
        "--server_ip", type=str, default="", help="For distant debugging."
    )
    parser.add_argument(
        "--server_port", type=str, default="", help="For distant debugging."
    )

    parser.add_argument(
        "--secs_per_example", type=int, default=10, help="Number of secs per example."
    )
    parser.add_argument(
        "--get_mc_states_name", type=str, default="binary_task", help=""
    )

    parser.add_argument("--same_movie", action="store_true", help="")
    parser.add_argument("--same_movie_temperature", type=float, default=0.2, help="")
    parser.add_argument("--same_movie_weight", type=float, default=1.0, help="")

    parser.add_argument("--train_long_term", action="store_true", help="")
    parser.add_argument("--train_long_term_linear", action="store_true", help="")
    parser.add_argument("--train_long_term_dropout", action="store_true", help="")

    parser.add_argument(
        "--long_term_task_name", type=str, default="relationship", help=""
    )
    parser.add_argument("--num_long_term_classes", type=int, default=-1, help="")

    parser.add_argument("--eval_epochs", default="", type=str, help="")

    parser.add_argument(
        "--num_workers", type=int, default=16, help="Number of DataLoader workers."
    )
    parser.add_argument(
        "--num_workers_eval", type=int, default=4, help="Number of DataLoader workers."
    )
    parser.add_argument(
        "--force_load_checkpoint",
        type=str,
        default="",
        help="Force-load checkpoint path.",
    )
    parser.add_argument(
        "--force_load_checkpoint_opt",
        type=str,
        default=None,
        help="Force-load checkpoint path.",
    )

    parser.add_argument("--init_final", action="store_true", help="")

    parser.add_argument(
        "--train_feature_file", default=None, type=str, required=True, help=""
    )
    parser.add_argument("--mc_train_feature_file", default=None, type=str, help="")
    parser.add_argument(
        "--eval_feature_file", default=None, type=str, required=True, help=""
    )

    parser.add_argument("--exp", default="", type=str, required=True, help="")
    parser.add_argument("--num_action_classes", type=int, default=80, help="")
    parser.add_argument("--max_position_embeddings", type=int, default=258, help="")
    parser.add_argument("--action_recognition", action="store_true", help="")
    parser.add_argument("--num_hidden_layers", type=int, default=3, help="")
    parser.add_argument("--num_attention_heads", type=int, default=12, help="")

    parser.add_argument("--action_feat_dim", type=int, default=2304, help="")
    parser.add_argument("--feat_dim", type=int, default=2304, help="")
    parser.add_argument("--action_loss_weight", default=1.0, type=float, help="")

    parser.add_argument("--no_link_ids", action="store_true", help="")
    parser.add_argument("--no_scene_ids", action="store_true", help="")
    parser.add_argument("--no_pos_ids", action="store_true", help="")

    parser.add_argument("--use_soft_labels", action="store_true", help="")
    parser.add_argument("--mask_sep", action="store_true", help="")
    parser.add_argument("--mask_sep_no_mask", action="store_true", help="")

    parser.add_argument("--temperature", default=1.0, type=float, help="")
    parser.add_argument("--eval_sample_x", default=10, type=int, help="")

    parser.add_argument("--three_split", action="store_true", help="")

    parser.add_argument(
        "--short_term_model_weights",
        default="/home/s222126678/Documents/lvu_trans/data/ava/SLOWFAST_32x2_R101_50_50.pkl",
        type=str,
        help="",
    )

    parser.add_argument("--debug", action="store_true", help="")
    parser.add_argument("--use_good_quality", action="store_true", help="")

    args = parser.parse_args()

    args.is_end_task = args.train_long_term or args.action_recognition

    args.all_feat_dims = [2304]
    if args.do_wandb:
        wandb.init(project="Modify LUV", name="Change LUV")
    if (
        args.model_type in ["bert", "roberta", "distilbert", "camembert"]
        and not args.mlm
    ):
        raise ValueError(
            "BERT and RoBERTa-like models do not have LM heads but masked LM heads. They must be run using the --mlm "
            "flag (masked language modeling)."
        )
    if args.eval_data_file is None and args.do_eval:
        raise ValueError(
            "Cannot do evaluation without an evaluation data file. Either supply a file to --eval_data_file "
            "or remove the --do_eval argument."
        )
    if (
        os.path.exists(args.output_dir)
        and os.listdir(args.output_dir)
        and args.do_train
        and not args.overwrite_output_dir
    ):
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args.output_dir
            )
        )

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device(
            "cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu"
        )
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        args.local_rank,
        device,
        args.n_gpu,
        bool(args.local_rank != -1),
        args.fp16,
    )

    # Set seed
    set_seed(args)
    config_class = RobertaConfig
    config = config_class()

    model_class = RobertaForMaskedLM
    logger.info("Training new model from scratch")
    model = model_class(config=config, args=args)
    model.to(args.device)

    if args.local_rank == 0:
        torch.distributed.barrier()  # End of barrier to make sure only the first process in distributed training download model & vocab

    logger.info("Training/evaluation parameters %s", args)
    global proj_W
    global proj_b

    tmp_state_dict = torch.load(
        args.short_term_model_weights,
        map_location="cpu",
    )
    proj_W = (
        torch.tensor(tmp_state_dict["model_state"]["head.projection.weight"].numpy())
        .float()
        .T
    )  # 2304, 80
    proj_b = torch.tensor(
        tmp_state_dict["model_state"]["head.projection.bias"].numpy()
    ).float()  # 80

    args.soft_label_dim = 80

    proj_W = proj_W.to(args.device)
    proj_b = proj_b.to(args.device)

    # Training
    if args.do_train:
        if args.local_rank not in [-1, 0]:
            torch.distributed.barrier()  # Barrier to make sure only the first process in distributed training process the dataset, and the others will use the cache

        train_dataset = VideoDataset(args, evaluate=False)

        if args.local_rank == 0:
            torch.distributed.barrier()

        global_step, tr_loss = train(args, train_dataset, model)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)
    wandb.finish()
    if args.is_end_task and args.local_rank in [-1, 0]:
        evaluate(args, model)


if __name__ == "__main__":
    main()
