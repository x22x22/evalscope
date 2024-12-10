# Copyright (c) Alibaba, Inc. and its affiliates.
"""
Run evaluation for LLMs.
"""
import os.path
from datetime import datetime
from typing import List, Union

import torch

from evalscope.arguments import parse_args
from evalscope.config import TaskConfig
from evalscope.constants import DEFAULT_MODEL_REVISION, DEFAULT_WORK_DIR, EvalBackend, OutputsStructure
from evalscope.evaluator import Evaluator, HumanevalEvaluator
from evalscope.models.custom import CustomModel
from evalscope.utils import dict_to_yaml, gen_hash, import_module_util, json_to_dict, seed_everything, yaml_to_dict
from evalscope.utils.logger import get_logger

logger = get_logger()

BENCHMARK_PATH_PREFIX = 'evalscope.benchmarks.'
MEMBERS_TO_IMPORT = ['DATASET_ID', 'SUBSET_LIST', 'DataAdapterClass', 'ModelAdapterClass']


def run_task(task_cfg: Union[str, dict, TaskConfig, List[TaskConfig]]) -> Union[dict, List[dict]]:
    run_time = datetime.now().strftime('%Y%m%d_%H%M%S')

    if isinstance(task_cfg, list):
        eval_results = []
        for one_task_cfg in task_cfg:
            eval_results.append(run_single_task(one_task_cfg, run_time))
        return eval_results

    if isinstance(task_cfg, TaskConfig):
        logger.info('Args: Task config is provided with TaskConfig type.')
    elif isinstance(task_cfg, dict):
        logger.info('Args: Task config is provided with dictionary type.')
        task_cfg = TaskConfig(**task_cfg)
    elif isinstance(task_cfg, str):
        if task_cfg.endswith('.yaml'):
            logger.info('Args: Task config is provided with yaml type.')
            task_cfg_dict = yaml_to_dict(task_cfg)
            task_cfg = TaskConfig(**task_cfg_dict)
        elif task_cfg.endswith('.json'):
            logger.info('Args: Task config is provided with json type.')
            task_cfg_dict = json_to_dict(task_cfg)
            task_cfg = TaskConfig(**task_cfg_dict)
        else:
            raise ValueError(f'Unsupported file format: {task_cfg}, should be a yaml or json file.')
    else:
        raise ValueError('Args: Please provide a valid task config.')

    result = run_single_task(task_cfg, run_time)
    return result


def run_single_task(task_cfg: TaskConfig, run_time: str) -> dict:

    seed_everything(task_cfg.seed)

    # Set work_dir
    if task_cfg.use_cache:
        task_cfg.work_dir = task_cfg.use_cache
        logger.info(f'Set resume from {task_cfg.work_dir}')
    elif task_cfg.work_dir == DEFAULT_WORK_DIR:
        task_cfg.work_dir = os.path.join(task_cfg.work_dir, run_time)

    logger.info(task_cfg)

    eval_backend = task_cfg.eval_backend
    eval_config = task_cfg.eval_config

    if eval_backend != EvalBackend.NATIVE:

        if eval_config is None:
            logger.warning(f'Got eval_backend {eval_backend}, but eval_config is not provided.')

        if eval_backend == EvalBackend.OPEN_COMPASS:
            from evalscope.backend.opencompass import OpenCompassBackendManager
            oc_backend_manager = OpenCompassBackendManager(config=eval_config)
            oc_backend_manager.run()
        elif eval_backend == EvalBackend.VLM_EVAL_KIT:
            from evalscope.backend.vlm_eval_kit import VLMEvalKitBackendManager
            vlm_eval_kit_backend_manager = VLMEvalKitBackendManager(config=eval_config)
            vlm_eval_kit_backend_manager.run()
        elif eval_backend == EvalBackend.RAG_EVAL:
            from evalscope.backend.rag_eval import RAGEvalBackendManager
            rag_eval_backend_manager = RAGEvalBackendManager(config=eval_config)
            rag_eval_backend_manager.run()
        elif eval_backend == EvalBackend.THIRD_PARTY:
            raise NotImplementedError(f'Not implemented for evaluation backend {eval_backend}')

        return dict()

    model_args = task_cfg.model_args
    generation_config = task_cfg.generation_config
    dry_run = task_cfg.dry_run
    model = task_cfg.model
    template_type = task_cfg.template_type
    eval_type = task_cfg.eval_type
    work_dir = task_cfg.work_dir
    use_cache = task_cfg.use_cache
    datasets = task_cfg.datasets
    dataset_args = task_cfg.dataset_args
    dataset_hub = task_cfg.dataset_hub
    dataset_dir = task_cfg.dataset_dir
    stage = task_cfg.stage
    limit = task_cfg.limit
    debug = task_cfg.debug

    if model is None or datasets is None:
        raise ValueError('Args: Please provide model and datasets.')

    if template_type:
        logger.warning(
            'DeprecatedWarning: template_type is deprecated, please use `--chat-template` for custom chat template instead.'  # noqa: E501
        )

    model_precision = model_args.get('precision', 'auto')
    if isinstance(model_precision, str) and model_precision != 'auto':
        model_precision = eval(model_precision)

    # Get outputs directory
    outputs = OutputsStructure(outputs_dir=work_dir)

    # Dump overall task config
    task_cfg_file = os.path.join(outputs.configs_dir, f'task_config_{gen_hash(str(task_cfg), bits=6)}.yaml')
    try:
        logger.info(f'Dump task config to {task_cfg_file}')
        dict_to_yaml(task_cfg.to_dict(), task_cfg_file)
    except Exception as e:
        logger.warning(f'Failed to dump overall task config: {e}')

    # Get model args
    if dry_run:
        model_id: str = 'dummy'
        model_revision: str = 'v1.0.0'
    elif eval_type == 'custom':
        model_id: str = 'default'
        model_revision: str = DEFAULT_MODEL_REVISION
    else:
        model_id: str = model
        model_revision: str = model_args.get('revision', DEFAULT_MODEL_REVISION)

    eval_results = dict()
    for dataset_name in datasets:
        # Get imported_modules
        imported_modules = import_module_util(BENCHMARK_PATH_PREFIX, dataset_name, MEMBERS_TO_IMPORT)

        if dataset_name == 'humaneval' and dataset_args.get('humaneval', {}).get('local_path') is None:
            raise ValueError('Please specify the local problem path of humaneval dataset in --dataset-args,'
                             'e.g. {"humaneval": {"local_path": "/to/your/path"}}, '
                             'And refer to https://github.com/openai/human-eval/tree/master#installation to install it,'
                             'Note that you need to enable the execution code in the human_eval/execution.py first.')

        if dry_run:
            from evalscope.models.dummy_chat_model import DummyChatModel
            model_adapter = DummyChatModel(model_cfg=dict())
        elif eval_type == 'custom':
            if not isinstance(model, CustomModel):
                raise ValueError(f'Expected evalscope.models.custom.CustomModel, but got {type(model)}.')
            from evalscope.models.model_adapter import CustomModelAdapter
            model_adapter = CustomModelAdapter(custom_model=model)
        else:
            # Init model adapter
            device_map = model_args.get('device_map', 'auto') if torch.cuda.is_available() else None
            model_adapter = imported_modules['ModelAdapterClass'](
                model_id=model_id, model_revision=model_revision, device_map=device_map, torch_dtype=model_precision)

        if dataset_name == 'humaneval':
            problem_file: str = dataset_args.get('humaneval', {}).get('local_path')

            evaluator = HumanevalEvaluator(
                problem_file=problem_file,
                model_id=model_id,
                model_revision=model_revision,
                model_adapter=model_adapter,
                outputs=outputs,
                is_custom_outputs_dir=False,
            )
        else:
            # CHECK dataset_args
            dataset_name_or_path: str = dataset_args.get(dataset_name,
                                                         {}).get('local_path') or imported_modules['DATASET_ID']

            in_prompt_template: str = dataset_args.get(dataset_name, {}).get('prompt_template', '')

            # Init data adapter
            few_shot_num: int = dataset_args.get(dataset_name, {}).get('few_shot_num', None)
            few_shot_random: bool = dataset_args.get(dataset_name, {}).get('few_shot_random', True)
            data_adapter = imported_modules['DataAdapterClass'](
                few_shot_num=few_shot_num,
                few_shot_random=few_shot_random,
                prompt_template=in_prompt_template,
            )

            in_subset_list: list = dataset_args.get(dataset_name, {}).get('subset_list',
                                                                          imported_modules['SUBSET_LIST'])
            logger.info(f'Evaluating on subsets for {dataset_name}: {in_subset_list}\n')

            evaluator = Evaluator(
                dataset_name_or_path=dataset_name_or_path,
                subset_list=in_subset_list,
                data_adapter=data_adapter,
                model_adapter=model_adapter,
                use_cache=use_cache,
                outputs=outputs,
                datasets_dir=dataset_dir,
                datasets_hub=dataset_hub,
                stage=stage,
                eval_type=eval_type,
                overall_task_cfg=task_cfg,
            )

        res_dict: dict = evaluator.eval(infer_cfg=generation_config, debug=debug, limit=limit)

        eval_results[dataset_name] = res_dict

    return eval_results


def main():
    task_cfg = parse_args()
    run_task(task_cfg)


if __name__ == '__main__':
    main()
