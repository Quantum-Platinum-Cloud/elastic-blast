#!/usr/bin/env python3
"""
src/elb/resources/quotas/quota-check.py - entry point to functionality to check
whether enough resources are available to run ElasticBLAST

Author: Christiam Camacho (camacho@ncbi.nlm.nih.gov)
Created: Mon 14 Sep 2020 09:58:36 AM EDT
"""
import configparser
import elb.config
from elb.resources.quotas.quota_aws_ec2_cf import ResourceCheckAwsEc2CloudFormation
from elb.resources.quotas.quota_aws_batch import ResourceCheckAwsBatch
from elb.aws import create_aws_config
from elb.elb_config import ElasticBlastConfig
from typing import Union

def check_resource_quotas(cfg: ElasticBlastConfig) -> None:
    """
    Check the resources needed in a Cloud Service Provider to ensure
    ElasticBLAST can operate.

    Pre-condition: cfg is a validated ElasticBLAST configuration object
    Post-condition: if at the time this function is invoked the resources
    requested can be met, the function will return, otherwise an exception will
    be raised.
    """
    if cfg.cloud_provider.cloud == elb.config.CSP.AWS:
        boto_cfg = create_aws_config(cfg.aws.region)
        ResourceCheckAwsEc2CloudFormation(boto_cfg)()
        ResourceCheckAwsBatch(boto_cfg)()
    elif cfg.cloud_provider.cloud == elb.config.CSP.GCP:
        raise NotImplementedError('Resource check for GCP is not implemented yet')
    else:
        raise NotImplementedError('Resource check for unknown cloud vendor')
