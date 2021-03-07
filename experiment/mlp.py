import argparse
import gzip
import logging
import mlflow
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F

from skilearn.metrics import balanced_accuracy_score
from torch.utils.data import DataLoader
from tqdm import tqdm, trange

from .dataset import MeliChallengeDataset
from .utils import DATASET_SIZES, RawDataProcessor, PadSequences


logging.basicConfig(
    format="%(asctime)s: %(levelname)s - %(message)s",
    level=logging.INFO
)


class MLPClassifier(nn.Module):
    def __init__(self,
                 pretrained_embeddings_path,
                 dictionary,
                 n_labels,
                 hidden_layers=[256, 128],
                 dropout=0.3,
                 vector_size=300,
                 freeze_embedings=True):
        super().__init__()
        embeddings_matrix = torch.randn(len(dictionary), vector_size)
        embeddings_matrix[0] = torch.zeros(vector_size)
        with gzip.open(pretrained_embeddings_path, "rt") as fh:
            next(fh)
            for line in fh:
                word, vector = line.strip().split(None, 1)
                if word in dictionary.token2id:
                    embeddings_matrix[dictionary.token2id[word]] =\
                        torch.FloatTensor([float(n) for n in vector.split()])
        self.embeddings = nn.Embedding.from_pretrained(embeddings_matrix,
                                                       freeze=freeze_embedings,
                                                       padding_idx=0)
        self.hidden_layers = [
            nn.Linear(vector_size, hidden_layers[0])
        ]
        for input_size, output_size in zip(hidden_layers[:-1], hidden_layers[1:]):
            self.hidden_layers.append(
                nn.Linear(input_size, output_size)
            )
        self.dropout = dropout
        self.hidden_layers = nn.ModuleList(self.hidden_layers)
        self.output = nn.Linear(hidden_layers[-1], n_labels)
        self.vector_size = vector_size

    def forward(self, x):
        x = self.embeddings(x)
        x = torch.mean(x, dim=1)
        for layer in self.hidden_layers:
            x = F.relu(layer(x))
            if self.dropout:
                x = F.dropout(x, self.dropout)
        x = self.output(x)
        return x


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-data",
                        help="Path to the the training dataset",
                        required=True)
    parser.add_argument("--pretrained-embeddings",
                        help="Path to the pretrained embeddings file.",
                        required=True)
    parser.add_argument("--language",
                        help="Language working with",
                        required=True)
    parser.add_argument("--test-data",
                        help="If given, use the test data to perform evaluation.")
    parser.add_argument("--validation-data",
                        help="If given, use the validation data to perform evaluation.")
    parser.add_argument("--embeddings-size",
                        default=300,
                        help="Size of the vectors.",
                        type=int)
    parser.add_argument("--hidden-layers",
                        help="Sizes of the hidden layers of the MLP (can be one or more values)",
                        nargs="+",
                        default=[256, 128],
                        type=int)
    parser.add_argument("--droptout",
                        help="Dropout to apply to each hidden layer",
                        default=0.3,
                        type=float)

    args = parser.parse_args()

    logging.info("Building processor")
    preprocess = RawDataProcessor(
        dataset_path=args.train_data,
        dataset_size=DATASET_SIZES[args.language]["train"],
        ignore_header=True,
        filters=None,  # Default filters
        vocab_size=50000  # This can be a hyperparameter
    )

    pad_sequences = PadSequences(
        pad_value=0,
        max_length=None,
        min_length=1
    )

    logging.info("Building training dataset")
    train_dataset = MeliChallengeDataset(
        dataset_path=args.train_data,
        dataset_size=DATASET_SIZES[args.language]["train"],
        random_buffer_size=2048,  # This can be a hypterparameter
        transform=preprocess
    )
    train_loader = DataLoader(
        train_dataset,
        batch_size=128,  # This can be a hyperparameter
        shuffle=False,
        collate_fn=pad_sequences,
        drop_last=False
    )

    if args.validation_data:
        logging.info("Building validation dataset")
        validation_dataset = MeliChallengeDataset(
            dataset_path=args.validation_data,
            dataset_size=DATASET_SIZES[args.language]["validation"],
            random_buffer_size=1,
            transform=preprocess
        )
        train_loader = DataLoader(
            train_dataset,
            batch_size=128,
            shuffle=False,
            collate_fn=pad_sequences,
            drop_last=False
        )
    else:
        validation_dataset = None
        validation_loader = None

    if args.test_data:
        logging.info("Building test dataset")
        test_dataset = MeliChallengeDataset(
            dataset_path=args.test_data,
            dataset_size=DATASET_SIZES[args.language]["test"],
            random_buffer_size=1,
            transform=preprocess
        )
        train_loader = DataLoader(
            train_dataset,
            batch_size=128,
            shuffle=False,
            collate_fn=pad_sequences,
            drop_last=False
        )
    else:
        test_dataset = None
        test_loader = None

    mlflow.set_experiment(f"diplodatos.{args.language}")

    with mlflow.start_run():
        logging.info("Starting experiment")
        # Log all relevent hyperparameters
        mlflow.log_params({
            "model_type": "Multilayer Perceptron",
            "embeddings": args.pretrained_embeddings,
            "hidden_layers": args.hidden_layers,
            "dropout": args.dropout,
            "embeddings_size": args.embeddings_size
        })
        device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

        logging.info("Building classifier")
        model = MLPClassifier(
            pretrained_embeddings_path=args.pretrained_embeddings,
            dictionary=preprocess.dictionary,
            n_labels=len(preprocess.target_to_idx),
            hidden_layers=args.hidden_layers,
            dropout=args.dropout,
            vector_size=args.embeddings_size,
            freeze_embedings=True  # This can be a hyperparameter
        )
        model = model.to(device)
        loss = nn.CrossEntropyLoss()
        optimizer = optim.Adam(
            model.parameters(),
            lr=1e-3,  # This can be a hyperparameter
            weight_decay=1e-5  # This can be a hyperparameter
        )

        logging.info("Training classifier")
        epochs = trange(3)  # This can be a hyperparameter
        for epoch in epochs:
            model.train()
            running_loss = []
            for idx, batch in enumerate(tqdm(train_loader)):
                optimizer.zero_grad()
                data = batch["data"].to(device)
                target = batch["target"].to(device)
                output = model(data)
                loss_value = loss(output, target)
                loss_value.backward()
                optimizer.step()
                running_loss.append(loss_value.item())
            mlflow.log_metric("train_loss", sum(running_loss) / len(running_loss), epoch)

            if validation_dataset:
                logging.info("Evaluating model on validation")
                model.eval()
                running_loss = []
                targets = []
                predictions = []
                for batch in tqdm(validation_loader):
                    data = batch["data"].to(device)
                    target = batch["target"].to(device)
                    output = model(data)
                    running_loss.append(
                        loss(output, target).item()
                    )
                    targets.extend(batch["target"].numpy())
                    predictions.extend(output.squeeze().cpu().detach().numpy())
                mlflow.log_metric("validation_loss", sum(running_loss) / len(running_loss), epoch)
                mlflow.log_metric("validation_bacc", balanced_accuracy_score(targets, predictions), epoch)

        if test_dataset:
            logging.info("Evaluating model on test")
            model.eval()
            running_loss = []
            targets = []
            predictions = []
            for batch in tqdm(test_loader):
                data = batch["data"].to(device)
                target = batch["target"].to(device)
                output = model(data)
                running_loss.append(
                    loss(output, target).item()
                )
                targets.extend(batch["target"].numpy())
                predictions.extend(output.squeeze().cpu().detach().numpy())
            mlflow.log_metric("test_loss", sum(running_loss) / len(running_loss), epoch)
            mlflow.log_metric("test_bacc", balanced_accuracy_score(targets, predictions), epoch)