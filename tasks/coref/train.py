import torch
import argparse
from model import CorefModel
from data import CorefDataset
import logging
from collections import OrderedDict
import hashlib
from os import path
import os

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.DEBUG)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-data_dir", type=str,
        default="/share/data/lang/users/freda/codebase/hackathon_2019/tasks/constituent/"
        "data/edges/ontonotes/coref")
    parser.add_argument(
        "-model_dir", type=str,
        default="/home/shtoshni/Research/hackathon_2019/tasks/coref/checkpoints")
    parser.add_argument("-batch_size", type=int, default=32)
    parser.add_argument("-eval_batch_size", type=int, default=32)
    parser.add_argument("-n_epochs", type=int, default=5)
    parser.add_argument("-lr", type=float, default=1e-4)
    parser.add_argument("-lr_tune", type=float, default=1e-5)
    parser.add_argument("-span_dim", type=int, default=256)
    parser.add_argument("-model", type=str, default="bert")
    parser.add_argument("-model_size", type=str, default="base")
    parser.add_argument("-fine_tune", default=False, action="store_true")
    parser.add_argument("-pool_method", default="avg", type=str)
    parser.add_argument("-train_frac", default=1.0, type=float,
                        help="Can reduce this for quick testing.")
    parser.add_argument("-seed", type=int, default=0, help="Random seed")
    parser.add_argument("-eval", default=False, action="store_true")

    hp = parser.parse_args()
    return hp


def save_model(model, optimizer, scheduler, steps_done, max_f1, location):
    """Save model."""
    torch.save({
        'steps_done': steps_done,
        'max_f1': max_f1,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'scheduler_state_dict': scheduler.state_dict(),
        'rng_state': torch.get_rng_state(),
    }, location)
    logging.info("Model saved at: %s" % (location))


def train(model, train_iter, val_iter, optimizer, optimizer_tune, scheduler,
          model_dir, best_model_dir, init_steps=0, num_steps=30000, max_f1=0):
    model.train()

    steps_done = init_steps
    EVAL_STEPS = 1000
    while (steps_done < num_steps):
        logging.info("Epoch started")
        for idx, batch_data in enumerate(train_iter):
            optimizer.zero_grad()
            loss = model(batch_data)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(
                model.parameters(), 5.0)

            optimizer.step()

            if optimizer_tune:
                optimizer_tune.step()

            steps_done += 1

            if (steps_done % EVAL_STEPS) == 0:
                logging.info("Evaluating at %d" % steps_done)
                f1 = eval(model, val_iter)
                logging.info(
                    "Val F1: %.3f Steps (in K): %d Loss: %.3f" %
                    (f1, steps_done//EVAL_STEPS, loss.item()))
                # Scheduler step
                scheduler.step(f1)

                if f1 > max_f1:
                    max_f1 = f1
                    logging.info("Max F1: %.3f" % max_f1)
                    location = path.join(best_model_dir, "model.pt")
                    save_model(model, optimizer, scheduler, steps_done, f1, location)

                location = path.join(model_dir, "model.pt")
                save_model(model, optimizer, scheduler, steps_done, f1, location)

        logging.info("Epoch done!")


def eval(model, val_iter):
    model.eval()

    tp = 0
    fp = 0
    tn = 0
    fn = 0
    eps = 1e-8
    with torch.no_grad():
        for batch_data in val_iter:
            label = batch_data.label.cuda().float()
            _, pred = model(batch_data)
            pred = (pred >= 0.5).float()

            tp += torch.sum(label * pred)
            tn += torch.sum((1 - label) * (1 - pred))
            fp += torch.sum((1 - label) * pred)
            fn += torch.sum(label * (1 - pred))

    recall = tp/(tp + fn + eps)
    precision = tp/(tp + fp + eps)

    f_score = (2 * recall * precision) / (recall + precision + eps)
    model.train()
    return f_score


def get_model_name(hp):
    opt_dict = OrderedDict()
    # Only include important options in hash computation
    imp_opts = ['model', 'model_size', 'batch_size',
                'n_epochs',  'fine_tune', 'span_dim', 'pool_method', 'train_frac',
                'seed', 'lr', 'lr_tune']
    hp_dict = vars(hp)
    for key in imp_opts:
        val = hp_dict[key]
        opt_dict[key] = val
        logging.info("%s\t%s" % (key, val))

    str_repr = str(opt_dict.items())
    hash_idx = hashlib.md5(str_repr.encode("utf-8")).hexdigest()
    model_name = "coref_" + str(hash_idx)
    return model_name


def final_eval(hp, best_model_dir, test_iter):
    location = path.join(best_model_dir, "model.pt")
    if path.exists(location):
        checkpoint = torch.load(location)
        model = CorefModel(**vars(hp)).cuda()
        model.load_state_dict(checkpoint['model_state_dict'])
        max_f1 = checkpoint['max_f1']
        test_f1 = eval(model, test_iter)

        logging.info("Val F1: %.3f" % max_f1)
        logging.info("Test F1: %.3f" % test_f1)


def main():
    hp = parse_args()

    # Setup model directories
    model_name = get_model_name(hp)
    model_path = path.join(hp.model_dir, model_name)
    best_model_path = path.join(model_path, 'best_models')
    if not path.exists(model_path):
        os.makedirs(model_path)
    if not path.exists(best_model_path):
        os.makedirs(best_model_path)

    # Set random seed
    torch.manual_seed(hp.seed)

    # Initialize the model
    model = CorefModel(**vars(hp)).cuda()

    # Load data
    logging.info("Loading data")
    train_iter, val_iter, test_iter = CorefDataset.iters(
        hp.data_dir, model.encoder, batch_size=hp.batch_size,
        eval_batch_size=hp.eval_batch_size, train_frac=hp.train_frac)
    logging.info("Data loaded")

    if hp.eval:
        final_eval(hp, best_model_path, test_iter)
    else:
        optimizer_tune = None
        if hp.fine_tune:
            # TODO(shtoshni): Fix the parameters stuff
            optimizer_tune = torch.optim.Adam(model.get_core_params(), lr=hp.lr_tune)
        optimizer = torch.optim.Adam(model.get_other_params(), lr=hp.lr)

        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode='max', patience=5, factor=0.5)
        steps_done = 0
        max_f1 = 0
        num_steps = (hp.n_epochs * len(train_iter.data())) // hp.batch_size
        logging.info("Total training steps: %d" % num_steps)

        location = path.join(best_model_path, "model.pt")
        if path.exists(location):
            logging.info("Loading previous checkpoint")
            checkpoint = torch.load(location)
            model.load_state_dict(checkpoint['model_state_dict'])
            optimizer.load_state_dict(
                checkpoint['optimizer_state_dict'])
            scheduler.load_state_dict(
                checkpoint['scheduler_state_dict'])
            steps_done = checkpoint['steps_done']
            max_f1 = checkpoint['max_f1']
            torch.set_rng_state(checkpoint['rng_state'])
            logging.info("Steps done: %d, Max F1: %.3f" % (steps_done, max_f1))

        train(model, train_iter, val_iter, optimizer, optimizer_tune, scheduler,
              model_path, best_model_path, init_steps=steps_done, max_f1=max_f1,
              num_steps=num_steps)

        final_eval(hp, best_model_path, test_iter)


if __name__ == '__main__':
    main()