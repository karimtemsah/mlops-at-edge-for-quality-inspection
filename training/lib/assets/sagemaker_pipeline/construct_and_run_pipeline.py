#!/bin/python


import argparse
import os
import logging
from sagemaker.workflow.parameters import (
    ParameterString, ParameterFloat
)

from pipeline_helper import run_pipeline
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.processing import ProcessingInput, ProcessingOutput
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.sklearn.processing import ScriptProcessor
from sagemaker.workflow.properties import PropertyFile
from sagemaker.workflow.step_collections import RegisterModel
from sagemaker.inputs import TrainingInput
from sagemaker.mxnet import MXNet
from sagemaker.workflow.execution_variables import ExecutionVariables
from sagemaker.workflow.functions import Join
from sagemaker.workflow.conditions import ConditionGreaterThanOrEqualTo
from sagemaker.workflow.condition_step import ConditionStep, JsonGet
from sagemaker.workflow.retry import (
    SageMakerJobExceptionTypeEnum,
    SageMakerJobStepRetryPolicy
)





def main():
    """The main harness that creates or updates and runs the pipeline.

    Creates or updates the pipeline and runs it.
    """
    parser = argparse.ArgumentParser(
        "Creates and runs the pipeline."
    )

    parser.add_argument(
        "--role",
        default=os.environ.get('PIPELINE_ROLE'),
        help="The pipeline execution role",
    )

    parser.add_argument(
        "--pipeline-assets-prefix",
        default=os.environ.get('PIPELINE_ASSETS_PREFIX'),
        help="The pipeline execution role",
    )

    parser.add_argument(
        "--preprocess-image",
        default=os.environ.get('PREPROCESS_IMAGE'),
        help="The preprocessing image",
    )

    parser.add_argument(
        "--training_instance_type",
        default=os.environ.get("TRAINING_INSTANCE_TYPE"),
        help="The instance type used for training"
    )

    parser.add_argument(
        "--feature-group-name",
        default=os.environ.get('FEATURE_GROUP_NAME'),
        help="The name of the feature group where features are stored",
    )

    parser.add_argument(
        "--model-package-group-name",
        default=os.environ.get('MODEL_PACKAGE_GROUP_NAME'),
        help="The name of the model package group where the final model is stored",
    )

    parser.add_argument(
        "--update_only",
        dest="update_only",
        action='store_true',
        help="will only update pipeline definition",
    )

    args = parser.parse_args()

    logging.info(f"Running run_pipeline script with args {args}")

    pipeline_config = {
        "tmp_artifacts_uri": f"{args.pipeline_assets_prefix}/training/tmp",
        "role": args.role,
        "model_path": f"{args.pipeline_assets_prefix}/training/output/model",
        "training_instance_type": args.training_instance_type,
        "preprocess_image": args.preprocess_image,
        "pipeline_assets_prefix": args.pipeline_assets_prefix,
        "feature_group_name": args.feature_group_name,
        "model_package_group_name": args.model_package_group_name        
    }

    pipeline = construct_pipeline(**pipeline_config)

    run_pipeline(pipeline=pipeline, role=args.role)


def construct_pipeline(tmp_artifacts_uri=None, role=None, model_path=None, preprocess_image=None, training_instance_type=None, pipeline_assets_prefix=None,feature_group_name = None, model_package_group_name = None):

    
    model_approval_status = ParameterString(
        name="ModelApprovalStatus",
        default_value="Approved",
    )

    param_num_epochs = ParameterString(
        name="NumEpochs",
        default_value="150",
    )

    param_map_threshhold = ParameterFloat(
        name="MaPThresholdToRegisterModel",
        default_value=0.4,
    )

    processor = ScriptProcessor(
        command=['python3'],
        image_uri=preprocess_image,
        instance_type="ml.m5.large",
        instance_count=1,
        base_job_name="quality-inspection-process",
        role=role,
    )

    preprocess_step = ProcessingStep(
        name="LoadAndPreprocessDataset",
        processor=processor,
        outputs=[
            ProcessingOutput(output_name="train",
                             source="/opt/ml/processing/output/train", destination=f'{tmp_artifacts_uri}/train-rec'),
            ProcessingOutput(output_name="validation",
                             source="/opt/ml/processing/output/validation", destination=f'{tmp_artifacts_uri}/validation-rec'),
            ProcessingOutput(output_name="test",
                             source="/opt/ml/processing/output/test", destination=f'{tmp_artifacts_uri}/test-rec'),
        ],
        code="../docker/preprocess.py",
        job_arguments=['--query-results-s3uri', Join(
            values=[pipeline_assets_prefix,  "runs", ExecutionVariables.PIPELINE_EXECUTION_ID, "feature-store-queries"], on='/'), '--feature-group-name',feature_group_name ]
    )

    s3_custom_code_upload_location = f"{pipeline_assets_prefix}/tmp/"

    yolo_estimator = MXNet(
        base_job_name="quality-inspection",
        entry_point="../docker/train_yolo.py",
        role=role,
        output_path=model_path,
        code_location=s3_custom_code_upload_location,
        instance_count=1,
        instance_type=training_instance_type,
        framework_version="1.8.0",
        py_version="py37",
        metric_definitions=[
            {'Name': 'train:Loss',
                'Regex': '.*ObjLoss=(.*), BoxCenterLoss=.*, BoxScaleLoss=.*, ClassLoss=.*'},
            {'Name': 'train:BoxCenterLoss',
                'Regex': '.*ObjLoss=.*, BoxCenterLoss=(.*), BoxScaleLoss=.*, ClassLoss=.*'},
            {'Name': 'train:BoxScaleLoss',
                'Regex': '.*ObjLoss=.*, BoxCenterLoss=.*, BoxScaleLoss=(.*), ClassLoss=.*'},
            {'Name': 'train:ClassLoss',
                'Regex': '.*ObjLoss=.*, BoxCenterLoss=.*, BoxScaleLoss=.*, ClassLoss=(.*)'},
            {'Name': 'validation:mAP', 'Regex': 'mAP=(.*)'},
            {'Name': 'class_scratch:mAP', 'Regex': 'scratch=(.*)'}
        ],
        hyperparameters={
            "num-epochs": param_num_epochs,
            "data-shape": "300,450",
            "gpus": "0",
            "num-workers": "1",
            "batch-size": 8,
            "warmup-epochs": 1,
            "no-mixup-epochs": 5,
        },
    )

    step_train = TrainingStep(
        name="Train-QualityInspection-Model",
        estimator=yolo_estimator,
        retry_policies=[
            SageMakerJobStepRetryPolicy(
                exception_types=[SageMakerJobExceptionTypeEnum.RESOURCE_LIMIT],
                expire_after_mins=120,
                interval_seconds=10,
                backoff_rate=2.0
            )
        ],
        inputs={
            "train": TrainingInput(
                input_mode="File",
                s3_data=preprocess_step.properties.ProcessingOutputConfig.Outputs[
                    "train"].S3Output.S3Uri,
            ),
            "val": TrainingInput(
                input_mode="File",
                s3_data=preprocess_step.properties.ProcessingOutputConfig.Outputs[
                    "validation"].S3Output.S3Uri,

            ),
        },
    )
    evaluation_report = PropertyFile(
        name="EvaluationReport",
        output_name="evaluation",
        path="evaluation.json",
    )

    step_eval = ProcessingStep(
        name="EvaluateModel",
        processor=processor,
        inputs=[
            ProcessingInput(
                source=step_train.properties.ModelArtifacts.S3ModelArtifacts,
                destination="/opt/ml/processing/model",
            ),
            ProcessingInput(
                source=preprocess_step.properties.ProcessingOutputConfig.Outputs[
                    "test"
                ].S3Output.S3Uri,
                destination="/opt/ml/processing/test",
            ),
        ],
        outputs=[
            ProcessingOutput(
                output_name="evaluation", source="/opt/ml/processing/evaluation"
            ),
        ],
        code=os.path.join("../docker/evaluate.py"),
        property_files=[evaluation_report]
    )

    step_register = RegisterModel(
        name="Register-Model",
        estimator=yolo_estimator,
        model_data=step_train.properties.ModelArtifacts.S3ModelArtifacts,
        content_types=["image/jpeg"],
        response_types=["application/json"],
        inference_instances=["ml.c5.large", "ml.m5.large"],
        transform_instances=["ml.m5.large"],
        model_package_group_name=model_package_group_name,
        approval_status=model_approval_status,
    )

    # condition step for evaluating model quality and branching execution
    cond_lte = ConditionGreaterThanOrEqualTo(
        left=JsonGet(
            step=step_eval,
            property_file=evaluation_report,
            json_path="metrics.map",
        ),
        right=param_map_threshhold,
    )

    step_cond = ConditionStep(
        name="CheckEvaluation",
        conditions=[cond_lte],
        if_steps=[step_register],
        else_steps=[],
    )

    pipeline_name = f"TrainingPipeline"

    pipeline = Pipeline(
        name=pipeline_name,
        parameters=[param_num_epochs, param_map_threshhold,
                    model_approval_status
                    ],
        steps=[preprocess_step,
               step_train, step_eval, step_cond],
    )
    return pipeline


if __name__ == "__main__":
    main()
