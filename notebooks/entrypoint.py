# Based on glounts/nursery/sagemaker_sdk/entrypoint_scripts/train_entry_point.py
import argparse
import json
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

from gluonts.core import serde
from gluonts.dataset.common import FileDataset, MetaData, TrainDatasets, load_datasets
from gluonts.dataset.repository import datasets
from gluonts.evaluation import Evaluator, backtest
from gluonts.model.deepar import DeepAREstimator
from gluonts.model.deepstate import DeepStateEstimator

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s %(message)s", datefmt="[%Y-%m-%d %H:%M:%S]",
)
logger = logging.getLogger(__name__)

# TODO: implement model_fn, input_fn, predict_fn, and output_fn !!
# TODO: segment script for readability


def klass_dict(klass: str, args=[], kwargs={}):
    return {"__kind__": "instance", "class": klass, "args": args.copy(), "kwargs": kwargs.copy()}


def get_kwargs(hp: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    prefix += "."
    prefix_len = len(prefix)
    kwargs = {k[prefix_len:]: v for k, v in hp.items() if k.startswith(prefix)}
    return kwargs


def deser_algo_args(hp: Dict[str, Any], deser_list=[]):
    kwargs = {k: v for k, v in hp.items() if "." not in k}
    for argname in deser_list:
        if argname in kwargs:
            klass_name = kwargs[argname]
            # kwargs[argname] = serde.decode(klass_dict(klass_name, [], get_kwargs(hp, argname)))
            kwargs[argname] = klass_dict(klass_name, [], get_kwargs(hp, argname))
    return kwargs


deser_args = {
    "gluonts.model.deepar.DeepAREstimator": ["trainer", "distr_output"],
    "gluonts.model.deepstate.DeepStateEstimator": [
        "trainer",
        "issm",
        "noise_std_bounds",
        "prior_cov_bounds",
        "innovation_bounds",
        # Optional[List[TimeFeature]],
    ],
    "gluonts.model.deep_factor.DeepFactorEstimator": ["trainer", "distr_output"],
    "gluonts.model.transformer.TransformerEstimator": ["trainer", "distr_output"],  # Optional[List[TimeFeature]]
    "gluonts.model.gp_forecaster.GaussianProcessEstimator": [
        "trainer",
        "kernel_output",
    ],  # Optional[List[TimeFeature]], also cardinality should autodetect from train data?
    # NPTS: see predictor for the args & kwargs.
    # NOTE:
    # - npts doesn't need training, and just straight away predict. However, we still need to use its estimator to fake
    #   the training to let this script treat this algo exactly the same way as the others, rather than having an edge
    #   case aka if-else (ugh so yucky).
    # - kernel_type accepts strings: 'exponential' or 'uniform'. See the enumeration KernelType, so we actually don't
    #   need to do anything with this kwarg.
    "gluonts.model.npts.NPTSEstimator": [],  # ["kernel_type"],
}


def merge_metadata_hp(hp: Dict[str, Any], metadata: MetaData) -> Dict[str, Any]:
    """Resolve values to inject to the estimator: is it the hp or the one from metadata.

    This function:
    - mitigates errors made by callers when inadvertantly specifies hyperparameters that shouldn't be done, e.g.,
      the frequency should follow how the data prepared.
    - uses some metadata values as defaults, unless stated otherwise by the hyperparameters.
    """
    hp = hp.copy()

    # Always use freq from dataset.
    if "freq" in hp and hp["freq"] != metadata.freq:
        freq_hp = hp["freq"]
        print(f"freq: set freq='{metadata.freq}' from metadata; ignore '{freq_hp}' from hyperparam.")
    hp["freq"] = metadata.freq

    # Use prediction_length hyperparameters, but if not specified then fallbacks/defaults to the one from metadata.
    if "prediction_length" not in hp:
        hp["prediction_length"] = metadata.prediction_length
        print(
            "prediction_length: no hyperparam, so set " f"prediction_length={metadata.prediction_length} from metadata"
        )

    # TODO: autoprobe cardinalities.
    warnings.warn("This implementation still ignores cardinality and static features in the metadata", RuntimeWarning)

    return hp


def train(args, algo_args):
    """Train a specified estimator on a specified dataset."""
    # Load data
    if args.s3_dataset is None:
        # load built in dataset
        logger.info("Downloading dataset %s", args.dataset)
        dataset = datasets.get_dataset(args.dataset)
    else:
        # load custom dataset
        logger.info("Loading dataset from %s", args.s3_dataset)
        s3_dataset_dir = Path(args.s3_dataset)
        dataset = load_datasets(metadata=s3_dataset_dir, train=s3_dataset_dir / "train", test=s3_dataset_dir / "test",)

    # Initialize estimator
    algo_args = merge_metadata_hp(algo_args, dataset.metadata)
    estimator_config = klass_dict(args.algo, [], deser_algo_args(algo_args, deser_args[args.algo]))
    estimator = serde.decode(estimator_config)
    logger.info("Estimator: %s", estimator)
    if args.stop_before == "train":
        logger.info("Early termination: before %s", args.stop_before)
        return

    logger.info("Starting model training.")
    predictor = estimator.train(dataset.train)
    forecast_it, ts_it = backtest.make_evaluation_predictions(
        dataset=dataset.test, predictor=predictor, num_samples=int(args.num_samples),
    )
    if args.stop_before == "eval":
        logger.info("Early termination: before %s", args.stop_before)
        return

    logger.info("Starting model evaluation.")
    evaluator = Evaluator(quantiles=eval(args.quantiles))

    agg_metrics, item_metrics = evaluator(ts_it, forecast_it, num_series=len(dataset.test))

    # required for metric tracking.
    for name, value in agg_metrics.items():
        logger.info(f"gluonts[metric-{name}]: {value}")

    # save the evaluation results
    metrics_output_dir = Path(args.output_data_dir)
    with open(metrics_output_dir / "agg_metrics.json", "w") as f:
        json.dump(agg_metrics, f)
    with open(metrics_output_dir / "item_metrics.csv", "w") as f:
        item_metrics.to_csv(f, index=False)

    # save the model
    model_output_dir = Path(args.model_dir)
    predictor.serialize(model_output_dir)


def parse_hyperparameters(hm) -> Dict[str, Any]:
    """Convert list of ['--name', 'value', ...] to { 'name': value}, where 'value' is converted to the nearest data type.

    Conversion follows the principle: "if it looks like a duck and quacks like a duck, then it must be a duck".
    """
    d = {}
    it = iter(hm)
    try:
        while True:
            key = next(it)[2:]
            value = next(it)
            d[key] = value
    except StopIteration:
        pass

    # Infer data types.
    dd = {k: infer_dtype(v) for k, v in d.items()}
    return dd


def infer_dtype(s):
    """Auto-cast string values to nearest matching datatype.

    Conversion follows the principle: "if it looks like a duck and quacks like a duck, then it must be a duck".
    Note that python 3.6 implements PEP-515 which allows '_' as thousand separators. Hence, on Python 3.6,
    '1_000' is a valid number and will be converted accordingly.
    """
    if s == "None":
        return None
    if s == "True":
        return True
    if s == "False":
        return False

    try:
        i = float(s)
        if ("." in s) or ("e" in s.lower()):
            return i
        else:
            return int(s)
    except:  # noqa:E722
        pass

    try:
        # If string is json, deser it.
        return json.loads(s)
    except:  # noqa:E722
        return s


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # an alternative way to load hyperparameters via SM_HPS environment variable.
    parser.add_argument("--sm-hps", type=json.loads, default=os.environ.get("SM_HPS", {}))

    # input data, output dir and model directories
    parser.add_argument("--model-dir", type=str, default=os.environ.get("SM_MODEL_DIR", "model"))
    parser.add_argument("--output-data_dir", type=str, default=os.environ.get("SM_OUTPUT_DATA_DIR", "output"))
    # parser.add_argument("--input-dir", type=str, default=os.environ.get("SM_INPUT_DIR", "input"))
    parser.add_argument("--s3-dataset", type=str, default=os.environ.get("SM_CHANNEL_S3-DATASET", None))
    parser.add_argument("--dataset", type=str, default=os.environ.get("SM_HP_DATASET", ""))
    parser.add_argument("--num-samples", type=int, default=os.environ.get("SM_HP_NUM_SAMPLES", 100))
    parser.add_argument(
        "--quantiles",
        type=str,
        default=os.environ.get("SM_HP_QUANTILES", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]),
    )
    parser.add_argument(
        "--algo", type=str, default=os.environ.get("SM_HP_ALGO", "gluonts.model.deepar.DeepAREstimator"),
    )
    # Debug/dev/test features; source code is the documentation hence, only for developers :).
    parser.add_argument("--stop_before", type=str, default="")

    args, train_args = parser.parse_known_args()
    algo_args = parse_hyperparameters(train_args)
    train(args, algo_args)
