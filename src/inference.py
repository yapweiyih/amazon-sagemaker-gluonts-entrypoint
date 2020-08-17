import smepu

import inspect
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import matplotlib.cbook
import numpy as np
from gluonts.dataset.common import DataEntry, ListDataset, TrainDatasets, load_datasets
from gluonts.dataset.repository import datasets
from gluonts.evaluation import backtest
from gluonts.model.forecast import Config, Forecast
from gluonts.model.predictor import Predictor
from pandas.tseries import offsets
from pandas.tseries.frequencies import to_offset

from gluonts_example.evaluator import MyEvaluator
from gluonts_example.util import hp2estimator, mkdir

warnings.filterwarnings("ignore", category=matplotlib.cbook.mplDeprecation)

# Setup logger must be done in the entrypoint script.
logger = smepu.setup_opinionated_logger(__name__)


def log1p_tds(dataset: TrainDatasets) -> TrainDatasets:
    """Create a new train datasets with targets log-transformed."""
    # Implementation note: currently, the only way is to eagerly load all timeseries in memory, and do the transform.
    train = ListDataset(dataset.train, freq=dataset.metadata.freq)
    log1p(train)

    if dataset.test is not None:
        test = ListDataset(dataset.test, freq=dataset.metadata.freq)
        log1p(test)
    else:
        test = None

    # fmt: off
    return TrainDatasets(
        dataset.metadata.copy(),  # Note: pydantic's deep copy.
        train=train,
        test=test
    )
    # fmt: on


def log1p(ds: ListDataset):
    """In-place log transformation."""
    for data_entry in ds:
        data_entry["target"] = np.log1p(data_entry["target"])


def expm1_and_clip_to_zero(_, yhat: np.ndarray):
    """Expm1, followed by clip at 0.0."""
    logger.debug("Before expm1: %s %s", yhat.shape, yhat)
    logger.debug("After expm1: %s %s", yhat.shape, np.expm1(yhat))

    return np.clip(np.expm1(yhat), a_min=0.0, a_max=None)


def clip_to_zero(_, yhat: np.ndarray):
    return np.clip(yhat, a_min=0.0, a_max=None)


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
        dataset = load_datasets(
            metadata=s3_dataset_dir / "metadata", train=s3_dataset_dir / "train", test=s3_dataset_dir / "test",
        )
        # Apply transformation if requested
        if args.y_transform == "log1p":
            dataset = log1p_tds(dataset)

    # Initialize estimator
    estimator = hp2estimator(args.algo, algo_args, dataset.metadata)
    logger.info("Estimator: %s", estimator)

    # Debug/dev/test milestone
    if args.stop_before == "train":
        logger.info("Early termination: before %s", args.stop_before)
        return

    # Probe the right kwarg for validation data.
    # - NPTSEstimator (or any based on DummyEstimator) uses validation_dataset=...
    # - Other estimators use validation_data=...
    candidate_kwarg = [k for k in inspect.signature(estimator.train).parameters if "validation_data" in k]
    kwargs = {"training_data": dataset.train}
    if len(candidate_kwarg) == 1:
        kwargs[candidate_kwarg[0]] = dataset.test
    else:
        kwargs["validation_data"] = dataset.test

    # Train
    logger.info("Starting model training.")
    # predictor = estimator.train(training_data=dataset.train, validation_data=dataset.test)
    predictor = estimator.train(**kwargs)
    # Save
    model_dir = mkdir(args.model_dir)
    predictor.serialize(model_dir)
    # Also record the y's transformation & inverse transformation.
    with open(os.path.join(args.model_dir, "y_transform.json"), "w") as f:
        if args.y_transform == "log1p":
            f.write('{"transform": "log1p", "inverse_transform": "expm1"}\n')
            predictor.output_transform = expm1_and_clip_to_zero
        else:
            f.write('{"transform": "noop", "inverse_transform": "clip_at_zero"}\n')
            predictor.output_transform = clip_to_zero

    # Debug/dev/test milestone
    if args.stop_before == "eval":
        logger.info("Early termination: before %s", args.stop_before)
        return

    # Backtesting
    logger.info("Starting model evaluation.")
    forecast_it, ts_it = backtest.make_evaluation_predictions(
        dataset=dataset.test, predictor=predictor, num_samples=args.num_samples,
    )

    # Compute standard metrics over all samples or quantiles, and plot each timeseries, all in one go!
    # Remember to specify gt_inverse_transform when computing metrics.
    logger.info("MyEvaluator: assume non-negative ground truths, hence no clip_to_zero performed on them.")
    gt_inverse_transform = np.expm1 if args.y_transform == "log1p" else None
    evaluator = MyEvaluator(
        out_dir=Path(args.output_data_dir),
        quantiles=args.quantiles,
        plot_transparent=bool(args.plot_transparent),
        gt_inverse_transform=gt_inverse_transform,
        clip_at_zero=True,
    )
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

    # Specific requirement: output wmape to a separate file.

    with open(metrics_output_dir / f"{freq_name(dataset.metadata.freq)}-wmapes.csv", "w") as f:
        warnings.warn(
            "wmape csv uses daily or weekly according to frequency string, "
            "hence 7D still results in daily rather than weekly."
        )
        wmape_metrics = item_metrics[["item_id", "wMAPE"]].rename(
            {"item_id": "category", "wMAPE": "test_wMAPE"}, axis=1
        )
        wmape_metrics.to_csv(f, index=False)


def freq_name(s):
    """Convert frequency string to friendly name.

    This implementation uses only frequency string, hence 7D still becomes daily. It's not smart enough yet to know
    that 7D equals to week.
    """
    offset = to_offset(s)
    if isinstance(offset, offsets.Day):
        return "daily"
    elif isinstance(offset, offsets.Week):
        return "weekly"
    raise ValueError(f"Unsupported frequency: {s}")


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


def model_fn(model_dir: Union[str, Path]) -> Predictor:
    """Load a glounts model from a directory.

    Args:
        model_dir (Union[str, Path]): a directory where model is saved.

    Returns:
        Predictor: A gluonts predictor.
    """
    predictor = Predictor.deserialize(Path(model_dir))

    # If model was trained on log-space, then forecast must be inverted before metrics etc.
    with open(os.path.join(model_dir, "y_transform.json"), "r") as f:
        y_transform = json.load(f)
        logger.info("model_fn: custom transformations = %s", y_transform)

        if y_transform["inverse_transform"] == "expm1":
            predictor.output_transform = expm1_and_clip_to_zero
        else:
            predictor.output_transform = clip_to_zero

        # Custom field
        predictor.pre_input_transform = log1p if y_transform["transform"] == "log1p" else None

    logger.info("predictor.pre_input_transform: %s", predictor.pre_input_transform)
    logger.info("predictor.output_transform: %s", predictor.output_transform)
    logger.info("model_fn() done; loaded predictor %s", predictor)

    return predictor


def transform_fn(
    model: Predictor,
    request_body: Union[str, bytes],
    content_type: str = "application/json",
    accept_type: str = "application/json",
    num_samples: int = 1000,
) -> Union[bytes, Tuple[bytes, str]]:
    # See https://sagemaker.readthedocs.io/en/stable/using_mxnet.html#use-transform-fn
    #
    # [As of this writing on 20200506]
    # Looking at sagemaker_mxnet_serving_container/handler_service.py [1], it turns out I must use transform_fn()
    # because my gluonts predictor is neither mx.module.BaseModule nor mx.gluon.block.Block.
    #
    # I suppose the model_fn documentation [2] can be updated also, to make it clear that if entrypoint does not use
    # transform_fn(), then model_fn() must returns object with similar type to what the default implementation does.
    #
    # [1] https://github.com/aws/sagemaker-mxnet-serving-container/blob/406c1f387d9800ed264b538bdbf9a30de68b6977/src/sagemaker_mxnet_serving_container/handler_service.py
    # [2] https://sagemaker.readthedocs.io/en/stable/using_mxnet.html#load-a-model
    deser_input: List[DataEntry] = _input_fn(request_body, content_type)
    fcast: List[Forecast] = _predict_fn(deser_input, model, num_samples=num_samples)
    ser_output: Union[bytes, Tuple[bytes, str]] = _output_fn(fcast, accept_type)
    return ser_output


# Because we use transform_fn(), make sure this entrypoint does not contain input_fn() during inference.
def _input_fn(request_body: Union[str, bytes], request_content_type: str = "application/json") -> List[DataEntry]:
    """Deserialize JSON-lines into Python objects.

    Args:
        request_body (str): Incoming payload.
        request_content_type (str, optional): Ignored. Defaults to "".

    Returns:
        List[DataEntry]: List of gluonts timeseries.
    """

    # [20200508] I swear: two days ago request_body was bytes, today's string!!!
    if isinstance(request_body, bytes):
        request_body = request_body.decode("utf-8")
    return [json.loads(line) for line in io.StringIO(request_body)]


# Because we use transform_fn(), make sure this entrypoint does not contain predict_fn() during inference.
def _predict_fn(input_object: List[DataEntry], model: Predictor, num_samples=1000) -> List[Forecast]:
    """Take the deserialized JSON-lines, then perform inference against the loaded model.

    Args:
        input_object (List[DataEntry]): List of gluonts timeseries.
        model (Predictor): A gluonts predictor.
        num_samples (int, optional): Number of forecast paths for each timeseries. Defaults to 1000.

    Returns:
        List[Forecast]: List of forecast results.
    """
    # Create ListDataset here, because we need to match their freq with model's freq.
    X = ListDataset(input_object, freq=model.freq)

    # Apply forward transformation to input data, before injecting it to the predictor.
    if model.pre_input_transform is not None:
        logger.debug("Before model.pre_input_transform: %s", X.list_data)
        model.pre_input_transform(X)
        logger.debug("After model.pre_input_transform: %s", X.list_data)

    it = model.predict(X, num_samples=num_samples)
    return list(it)


# Because we use transform_fn(), make sure this entrypoint does not contain output_fn() during inference.
def _output_fn(
    forecasts: List[Forecast],
    content_type: str = "application/json",
    config: Config = Config(quantiles=["0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9"]),
) -> Union[bytes, Tuple[bytes, str]]:
    """Take the prediction result and serializes it according to the response content type.

    Args:
        prediction (List[Forecast]): List of forecast results.
        content_type (str, optional): Ignored. Defaults to "".

    Returns:
        List[str]: List of JSON-lines, each denotes forecast results in quantiles.
    """

    # jsonify_floats is taken from gluonts/shell/serve/util.py
    #
    # The module depends on flask, and we may not want to import when testing in our own dev env.
    def jsonify_floats(json_object):
        """Traverse through the JSON object and converts non JSON-spec compliant floats(nan, -inf, inf) to string.

        Parameters
        ----------
        json_object
            JSON object
        """
        if isinstance(json_object, dict):
            return {k: jsonify_floats(v) for k, v in json_object.items()}
        elif isinstance(json_object, list):
            return [jsonify_floats(item) for item in json_object]
        elif isinstance(json_object, float):
            if np.isnan(json_object):
                return "NaN"
            elif np.isposinf(json_object):
                return "Infinity"
            elif np.isneginf(json_object):
                return "-Infinity"
            return json_object
        return json_object

    str_results = "\n".join((json.dumps(jsonify_floats(forecast.as_json_dict(config))) for forecast in forecasts))
    bytes_results = str.encode(str_results)
    return bytes_results, content_type


if __name__ == "__main__":
    # Minimal argparser for SageMaker protocols
    parser = smepu.argparse.sm_protocol(channels=["s3_dataset", "dataset"])

    # SageMaker protocols: input data, output dir and model directories
    # parser.add_argument("--s3_dataset", type=str, default=os.environ.get("SM_CHANNEL_S3_DATASET", None))
    # parser.add_argument("--dataset", type=str, default=os.environ.get("SM_HP_DATASET", ""))

    # Arguments for evaluators
    parser.add_argument("--num_samples", type=int, default=os.environ.get("SM_HP_NUM_SAMPLES", 1000))
    parser.add_argument(
        "--quantiles", default=os.environ.get("SM_HP_QUANTILES", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
    )
    parser.add_argument(
        "--algo", type=str, default=os.environ.get("SM_HP_ALGO", "gluonts.model.deepar.DeepAREstimator")
    )
    parser.add_argument("--y_transform", type=str, default="noop", choices=["noop", "log1p"])

    # Argumets for plots
    parser.add_argument("--plot_transparent", type=int, default=os.environ.get("SM_HP_PLOT_TRANSPARENT", 0))

    # Debug/dev/test features; source code is the documentation hence, only for developers :).
    parser.add_argument("--stop_before", type=str, default="")

    logger.info("CLI args to entrypoint script: %s", sys.argv)
    args, train_args = parser.parse_known_args()
    algo_args = parse_hyperparameters(train_args)
    train(args, algo_args)