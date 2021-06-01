#                           PUBLIC DOMAIN NOTICE
#              National Center for Biotechnology Information
#  
# This software is a "United States Government Work" under the
# terms of the United States Copyright Act.  It was written as part of
# the authors' official duties as United States Government employees and
# thus cannot be copyrighted.  This software is freely available
# to the public for use.  The National Library of Medicine and the U.S.
# Government have not placed any restriction on its use or reproduction.
#   
# Although all reasonable efforts have been taken to ensure the accuracy
# and reliability of the software and data, the NLM and the U.S.
# Government do not and cannot warrant the performance or results that
# may be obtained by using this software or data.  The NLM and the U.S.
# Government disclaim all warranties, express or implied, including
# warranties of performance, merchantability or fitness for any particular
# purpose.
#   
# Please cite NCBI in any work or product based on this material.

"""
Help functions to access AWS resources and manipulate parameters and environment

"""

import getpass
import logging
import time
import os
from collections import defaultdict
import json
from tempfile import NamedTemporaryFile
import uuid

from pprint import pformat
from pathlib import Path
from .util import convert_labels_to_aws_tags, convert_disk_size_to_gb
from .util import convert_memory_to_mb, UserReportError
from .util import ElbSupportedPrograms
from .util import get_usage_reporting
from .util import UserReportError, sanitize_aws_batch_job_name
from .constants import BLASTDB_ERROR, CLUSTER_ERROR, ELB_AWS_QUERY_LENGTH, PERMISSIONS_ERROR
from .constants import ELB_DFLT_AWS_REGION, ELB_QUERY_BATCH_DIR, ELB_METADATA_DIR, ELB_LOG_DIR
from .constants import UNKNOWN_ERROR, ELB_DOCKER_IMAGE, CSP, INPUT_ERROR
from .constants import DEPENDENCY_ERROR, TIMEOUT_ERROR
from .constants import ELB_AWS_JOB_IDS
from .filehelper import parse_bucket_name_key
from .aws_traits import get_machine_properties, create_aws_config, get_availability_zones_for
from .base import InstanceProperties, DBSource
from .elb_config import ElasticBlastConfig

import boto3  # type: ignore
from botocore.config import Config  # type: ignore
from botocore.exceptions import ClientError, ParamValidationError, WaiterError # type: ignore

from typing import Dict, List, Tuple, Optional

    
CF_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'elastic-blast-cf.yaml')
# the order of job states reflects state transitions and is important for
# ElasticBlastAws.get_job_ids method
AWS_BATCH_JOB_STATES = ['SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING', 'RUNNING', 'SUCCEEDED', 'FAILED']


class ElasticBlastAws:
    """ Implementation of core ElasticBLAST functionality in AWS.
    Uses a CloudFormation template and AWS Batch.
    """

    def __init__(self, cfg: ElasticBlastConfig, create=False):
        """ Class constructor: it's meant to be a starting point and to implement
        a base class with the core ElasticBLAST interface
        Parameters:
            cfg - configuration to use for cluster creation
            create - if cluster does not exist, create it. Default: False
        """
        try:
            self._init(cfg, create)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code', 'Unknown') == 'AccessDenied':
                code = PERMISSIONS_ERROR
            else:
                code = CLUSTER_ERROR
            raise UserReportError(code, str(e))

    def _init(self, cfg: ElasticBlastConfig, create: bool):
        """ Internal constructor, not protected against exceptions in AWS """
        self.boto_cfg = create_aws_config(cfg.aws.region)
        self.cfg = cfg

        self.dry_run = self.cfg.cluster.dry_run
        self.stack_name = self.cfg.cluster.name

        self.cf = boto3.resource('cloudformation', config=self.boto_cfg)
        self.batch = boto3.client('batch', config=self.boto_cfg)
        self.s3 = boto3.resource('s3', config=self.boto_cfg)
        self.iam = boto3.resource('iam', config=self.boto_cfg)
        self.ec2 = boto3.resource('ec2', config=self.boto_cfg)

        self.owner = getpass.getuser()
        self.results_bucket = cfg.cluster.results
        self.vpc_id = cfg.aws.vpc
        self.subnets = None
        self._provide_subnets()
        self.cf_stack = None
        self.job_ids : List[str] = []

        initialized = True

        # Early check before creating cluster
        if create and self.cfg.blast.db:
            self.db, self.db_path, self.db_label = self._get_blastdb_info()

        try:
            if not self.dry_run:
                cf_stack = self.cf.Stack(self.stack_name)
                status = cf_stack.stack_status  # Will throw exception if error/non-existant
                self.cf_stack = cf_stack
                logging.debug(f'Initialized AWS CloudFormation stack {self.cf_stack}: status {status}')
            else:
                logging.debug(f'dry-run: would have initialized {self.stack_name}')
        except ClientError:
            initialized = False
        if not initialized and create:
            tags = convert_labels_to_aws_tags(self.cfg.cluster.labels)
            disk_size = convert_disk_size_to_gb(self.cfg.cluster.pd_size)
            instance_type = self.cfg.cluster.machine_type
            if instance_type.lower() == 'optimal':  # EXPERIMENTAL!
                max_cpus = self.cfg.cluster.num_nodes * self.cfg.cluster.num_cpus
            else:
                max_cpus = self.cfg.cluster.num_nodes * \
                    get_machine_properties(instance_type, self.boto_cfg).ncpus
            token = cfg.cluster.results.md5
            params = [
                {'ParameterKey': 'Owner', 'ParameterValue': self.owner},
                {'ParameterKey': 'MaxCpus', 'ParameterValue': str(max_cpus)},
                {'ParameterKey': 'MachineType', 'ParameterValue': instance_type},
                {'ParameterKey': 'DiskType', 'ParameterValue': self.cfg.cluster.disk_type},
                {'ParameterKey': 'DiskSize', 'ParameterValue': str(disk_size)},
                {'ParameterKey': 'Image', 'ParameterValue': ELB_DOCKER_IMAGE},
                {'ParameterKey': 'RandomToken', 'ParameterValue': token}
            ]
            if self.vpc_id and self.vpc_id.lower() != 'none':
                params.append({'ParameterKey': 'VPC', 'ParameterValue': self.vpc_id})
            else:
                azs = get_availability_zones_for(cfg.aws.region)
                params.append({'ParameterKey': 'NumberOfAZs', 'ParameterValue': str(len(azs))})
            if self.subnets:
                params.append({'ParameterKey': 'Subnets', 'ParameterValue': self.subnets})
            if cfg.aws.security_group and \
                    len(cfg.aws.security_group) > 0:
                params.append({'ParameterKey': 'SecurityGrp',
                               'ParameterValue': cfg.aws.security_group})
            if cfg.aws.key_pair:
                params.append({'ParameterKey': 'EC2KeyPair',
                               'ParameterValue': cfg.aws.key_pair})
            if self.cfg.cluster.iops:
                params.append({'ParameterKey': 'ProvisionedIops', 
                               'ParameterValue': str(self.cfg.cluster.iops)})

            instance_role = self._get_instance_role()
            batch_service_role = self._get_batch_service_role()
            job_role = self._get_job_role()
            spot_fleet_role = self._get_spot_fleet_role()

            if instance_role:
                params.append({'ParameterKey': 'InstanceRole',
                               'ParameterValue': instance_role})

            if batch_service_role:
                params.append({'ParameterKey': 'BatchServiceRole',
                               'ParameterValue': batch_service_role})

            if job_role:
                params.append({'ParameterKey': 'JobRole',
                               'ParameterValue': job_role})

            use_spot_instances = self.cfg.cluster.use_preemptible
            params.append({'ParameterKey': 'UseSpotInstances',
                           'ParameterValue': str(use_spot_instances)})
            if use_spot_instances:
                params.append({'ParameterKey': 'SpotBidPercentage',
                               'ParameterValue': str(self.cfg.cluster.bid_percentage)})
                if spot_fleet_role:
                    params.append({'ParameterKey': 'SpotFleetRole',
                                   'ParameterValue': str(spot_fleet_role)})

            capabilities = []
            if not (instance_role and batch_service_role and job_role and spot_fleet_role):
                # this is needed if cloudformation template creates roles
                capabilities = ['CAPABILITY_NAMED_IAM']

            logging.debug(f'Setting AWS tags: {pformat(tags)}')
            logging.debug(f'Setting AWS CloudFormation parameters: {pformat(params)}')
            logging.debug(f'Creating CloudFormation stack {self.stack_name} from {CF_TEMPLATE}')
            template_body = Path(CF_TEMPLATE).read_text()
            if not self.dry_run:
                self.cf_stack = self.cf.create_stack(StackName=self.stack_name,
                                                     TemplateBody=template_body,
                                                     Parameters=params,
                                                     Tags=tags,
                                                     Capabilities=capabilities)
                waiter = self.cf.meta.client.get_waiter('stack_create_complete')
                try:
                    # Waiter periodically probes for cloudformation stack
                    # status with default period of 30s and 120 tries.
                    # If it takes over an hour to create a stack, then the code
                    # will exit with an error before the stack is created.
                    waiter.wait(StackName=self.stack_name)
                except WaiterError as err:
                    # report cloudformation stack creation timeout
                    if self.cf_stack.stack_status == 'CREATE_IN_PROGRESS':
                        raise UserReportError(returncode=TIMEOUT_ERROR,
                                              message='Cloudforation stack creation has timed out')

                    # report cloudformation stack creation error,
                    elif self.cf_stack.stack_status != 'CREATE_COMPLETE':
                        # report error message
                        message = 'Cloudformation stack creation failed'
                        stack_messages = self._get_cloudformation_errors()
                        if stack_messages:
                            message += f' with error message {". ".join(stack_messages)}'
                        else:
                            message += f' for unknown reason.'
                        message += ' Please, run elastic-blast delete to remove cloudformation stack with errors'
                        raise UserReportError(returncode=DEPENDENCY_ERROR,
                                              message=message)

                status = self.cf_stack.stack_status
                logging.debug(f'Created AWS CloudFormation stack {self.cf_stack}: status {status}')

            else:
                logging.debug(f'dry-run: would have registered CloudFormation template {template_body}')

        # get job queue name and job definition name from cloudformation stack
        # outputs
        self.job_queue_name = None
        self.job_definition_name = None
        if not self.dry_run and self.cf_stack and \
               self.cf_stack.stack_status == 'CREATE_COMPLETE':
            for output in self.cf_stack.outputs:
                if output['OutputKey'] == 'JobQueueName':
                    self.job_queue_name = output['OutputValue']
                elif output['OutputKey'] == 'JobDefinitionName':
                    self.job_definition_name = output['OutputValue']

            if self.job_queue_name:
                logging.debug(f'JobQueueName: {self.job_queue_name}')
            else:
                raise UserReportError(returncode=DEPENDENCY_ERROR, message='JobQueueName could not be read from cloudformation stack')

            if self.job_definition_name:
                logging.debug(f'JobDefinitionName: {self.job_definition_name}')
            else:
                raise UserReportError(returncode=DEPENDENCY_ERROR, message='JobDefinitionName could not be read from cloudformation stack')

    def _provide_subnets(self):
        """ Read subnets from config file or if not set try to get them from default VPC """
        if not self.cfg.aws.subnet:
            logging.debug("Subnets are not provided")
            # Try to get subnet from default VPC or VPC set in aws-vpc config parameter
            vpc = self._provide_vpc()
            if vpc:
                subnet_list = vpc.subnets.all()
                self.vpc_id = vpc.id
                self.subnets = ','.join(map(lambda x: x.id, subnet_list))
        else:
            # Ensure that VPC is set and that subnets provided belong to it
            subnets = [x.strip() for x in self.cfg.aws.subnet.split(',')]
            # If aws-vpc parameter is set, use this VPC, otherwise use VPC of the
            # first subnet
            logging.debug(f"Subnets are provided: {' ,'.join(subnets)}")
            vpc = None
            if self.vpc_id:
                if self.vpc_id.lower() == 'none':
                    return None
                vpc = self.ec2.Vpc(self.vpc_id)
            for subnet_name in subnets:
                subnet = self.ec2.Subnet(subnet_name)
                if not vpc:
                    vpc = subnet.vpc # if subnet is invalid - will throw an exception botocore.exceptions.ClientError with InvalidSubnetID.NotFound
                else:
                    if subnet.vpc != vpc:
                        raise UserReportError(returncode=INPUT_ERROR, message="Subnets set in aws-subnet parameter belong to different VPCs")
            self.vpc_id = vpc.id
            self.subnets = ','.join(subnets)
        logging.debug(f"Using VPC {self.vpc_id}, subnet(s) {self.subnets}")

    def _provide_vpc(self):
        """ Get boto3 Vpc object for either configured VPC, or if not, default VPC for the
            configured region, if not available return None """
        if self.vpc_id:
            if self.vpc_id.lower() == 'none':
                return None
            return self.ec2.Vpc(self.vpc_id)
        vpcs = list(self.ec2.vpcs.filter(Filters=[{'Name':'isDefault', 'Values':['true']}]))
        if len(vpcs) > 0:
            logging.debug(f'Default vpc is {vpcs[0].id}')
            return vpcs[0]
        else:
            return None

    def _get_instance_role(self) -> str:
        """Find role for AWS ECS instances.
        Returns:
            * cfg.aws.instance_role value in config, if provided,
            * otherwise, ecsInstanceRole if this role and instance profile exist
            in AWS account,
            * otherwise, an empty string"""

        # if instance role is set in config, return it
        if self.cfg.aws.instance_role:
            logging.debug(f'Instance role provided from config: {self.cfg.aws.instance_role}')
            return self.cfg.aws.instance_role

        # check if ecsInstanceRole is present in the account and return it,
        # if it is
        # instance profile and role, both named ecsInstanceRole must exist
        DFLT_INSTANCE_ROLE_NAME = 'ecsInstanceRole'
        instance_profile = self.iam.InstanceProfile(DFLT_INSTANCE_ROLE_NAME)
        try:
            role_names = [i.name for i in instance_profile.roles]
            if DFLT_INSTANCE_ROLE_NAME in role_names:
                logging.debug(f'Using {DFLT_INSTANCE_ROLE_NAME} present in the account')
                return DFLT_INSTANCE_ROLE_NAME
        except self.iam.meta.client.exceptions.NoSuchEntityException:
            # an exception means that ecsInstanceRole is not defined in the
            # account
            pass

        # otherwise return en empty string, which cloudformation template
        # will interpret to create the instance role
        logging.debug('Instance role will be created by cloudformation')
        return ''

    def _get_batch_service_role(self):
        """Find AWS Batch service role.
        Returns:
            * cfg.aws.batch_service_role value in config, if provided,
            * otherwise, AWSBatchServiceRole if this role if it exists in AWS account,
            * otherwise, an empty string"""
        # if batch service role is set in config, return it
        if self.cfg.aws.batch_service_role:
            logging.debug(f'Batch service role provided from config: {self.cfg.aws.batch_service_role}')
            return self.cfg.aws.batch_service_role

        # check if ecsInstanceRole is present in the account and return it,
        # if it is
        # instance profile and role, both named ecsInstanceRole must exist
        DFLT_BATCH_SERVICE_ROLE_NAME = 'AWSBatchServiceRole'
        role = self.iam.Role(DFLT_BATCH_SERVICE_ROLE_NAME)
        try:
            role.arn
            logging.debug(f'Using {role.name} present in the account')
            return role.arn
        except self.iam.meta.client.exceptions.NoSuchEntityException:
            # an exception means that the role is not defined in the account
            pass

        # otherwise return en empty string, which cloudformation template
        # will interpret to create the instance role
        logging.debug('Batch service role will be created by cloudformation')
        return ''

    def _get_job_role(self):
        """Find AWS Batch job role.
        Returns:
            cfg.aws.job_role value in config, if provided,
            otherwise, an empty string"""
        if self.cfg.aws.job_role:
            job_role = self.cfg.aws.job_role
            logging.debug(f'Using Batch job role provided from config: {job_role}')
            return job_role
        else:
            logging.debug('Batch job role will be created by cloudformation')
            return ''

    def _get_spot_fleet_role(self):
        """Find AWS EC2 Spot Fleet role.
        Returns:
            cfg.aws.spot_fleet_role value in config, if provided,
            otherwise, an empty string"""
        if self.cfg.aws.spot_fleet_role:
            role = self.cfg.aws.spot_fleet_role
            logging.debug(f'Using Spot Fleet role provided from config: {role}')
            return role
        else:
            logging.debug('Spot Fleet role will be created by cloudformation')
            return ''

    def delete(self):
        """Delete a CloudFormation stack associated with AWS Batch resources."""
        try:
            self._delete()
        except ClientError as e:
            if e.response.get('Error', {}).get('Code', 'Unknown') == 'AccessDenied':
                code = PERMISSIONS_ERROR
            else:
                code = CLUSTER_ERROR
            raise UserReportError(code, str(e))

    def _delete(self):
        """ Internal delete, not protected against exceptions in AWS """
        if not self.dry_run:
            if not self.cf_stack:
                logging.info(f"AWS CloudFormation stack {self.stack_name} doesn't exist, nothing to delete")
                return
            logging.debug(f'Deleting AWS CloudFormation stack {self.stack_name}')
            self.cf_stack.delete()
            for sd in [ELB_QUERY_BATCH_DIR, ELB_METADATA_DIR, ELB_LOG_DIR]:
                self._remove_ancillary_data(sd)
            waiter = self.cf.meta.client.get_waiter('stack_delete_complete')
            try:
                waiter.wait(StackName=self.stack_name)
            except WaiterError:
                # report cloudformation stack deletion timeout
                if self.cf_stack.stack_status == 'DELETE_IN_PROGRESS':
                    raise UserReportError(returncode=TIMEOUT_ERROR,
                                          message='Cloudformation stack deletion has timed out')

                # report cloudformation stack deletion error
                elif self.cf_stack.stack_status != 'DELETE_COMPLETE':
                    message = 'Cloudformation stack deletion failed'
                    stack_messages = self._get_cloudformation_errors()
                    if stack_messages:
                        message += f' with errors {". ".join(stack_messages)}'
                    else:
                        message += ' for unknown reason'
                    raise UserReportError(returncode=DEPENDENCY_ERROR,
                                          message=message)
            logging.debug(f'Deleted AWS CloudFormation stack {self.stack_name}')
        else:
            logging.debug(f'dry-run: would have deleted {self.stack_name}')

    def _get_blastdb_info(self) -> Tuple[str, str, str]:
        """Returns a tuple of BLAST database basename, path (if applicable), and label
        suitable for job name. Gets user provided database from configuration.
        For custom database finds basename from full path, and provides
        correct path for db retrieval.
        For standard database the basename is the only value provided by the user,
        and the path name returned is empty.
        Example
        cfg.blast.db = pdb_nt -> 'pdb_nt', 'None', 'pdb_nt'
        cfg.blast.db = s3://example/pdb_nt -> 'pdb_nt', 's3://example', 'pdb_nt'
        """
        db = self.cfg.blast.db
        db_path = 'None'
        if db.startswith('s3://'):
            #TODO: support tar.gz database
            bname, key = parse_bucket_name_key(db)
            if not self.dry_run:
                try:
                    bucket = self.s3.Bucket(bname)
                    if len(list(bucket.objects.filter(Prefix=key, Delimiter='/'))) == 0:
                        raise RuntimeError
                except:
                    raise UserReportError(returncode=BLASTDB_ERROR,
                                          message=f'{db} is not a valid BLAST database')
            db_path = os.path.dirname(db)
            db = os.path.basename(db)
        elif db.startswith('gs://'):
            raise UserReportError(returncode=BLASTDB_ERROR,
                                  message=f'User database should be in the AWS S3 bucket')

        return db, db_path, sanitize_aws_batch_job_name(db)

    def submit(self, query_batches):
        """ Initial attempt to submit an AWS Batch search.
        Starting point to further refactorings.
        """
        try:
            self._submit(query_batches)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code', 'Unknown') == 'AccessDenied':
                code = PERMISSIONS_ERROR
            else:
                code = CLUSTER_ERROR
            raise UserReportError(code, str(e))

    def _submit(self, query_batches):
        """ Internal submit, not protected against exceptions in AWS """
        self.job_ids = []

        prog = self.cfg.blast.program

        if self.cfg.blast.db_source != DBSource.AWS:
            logging.warning(f'BLAST databases for AWS based ElasticBLAST obtained from {self.cfg.blast.db_source.name}')

        overrides = {
            'vcpus': self.cfg.cluster.num_cpus,
            'memory': int(convert_memory_to_mb(self.cfg.blast.mem_limit))
        }
        usage_reporting = get_usage_reporting()
        elb_job_id = uuid.uuid4().hex

        parameters = {'db': self.db,
                      'db-path': self.db_path,
                      'db-source': self.cfg.blast.db_source.name,
                      'db-mol-type': ElbSupportedPrograms().get_molecule_type(prog),
                      'num-vcpus': str(self.cfg.cluster.num_cpus),
                      'blast-program': prog,
                      'blast-options': self.cfg.blast.options,
                      'bucket': self.results_bucket}

        if self.cfg.blast.taxidlist:
            parameters['taxidlist'] = self.cfg.blast.taxidlist
        
        logging.debug(f'Job definition container overrides {overrides}')

        for i, q in enumerate(query_batches):
            parameters['query-batch'] = q
            jname = f'elasticblast-{self.owner}-{prog}-batch-{self.db_label}-job-{i}'
            # add random search id for ElasticBLAST usage reporting
            # and pass BLAST_USAGE_REPORT environment var to container
            if usage_reporting:
                overrides['environment'] = [{'name': 'BLAST_ELB_JOB_ID',
                                             'value': elb_job_id},
                                            {'name': 'BLAST_USAGE_REPORT',
                                             'value': 'true'},
                                            {'name': 'BLAST_ELB_BATCH_NUM',
                                             'value': str(i)}]
            else:
                overrides['environment'] = [{'name': 'BLAST_USAGE_REPORT',
                                             'value': 'false'}]
            if not self.dry_run:
                job = self.batch.submit_job(jobQueue=self.job_queue_name,
                                            jobDefinition=self.job_definition_name,
                                            jobName=jname,
                                            parameters=parameters,
                                            containerOverrides=overrides)
                self.job_ids.append(job['jobId'])
                logging.debug(f"Job definition parameters for job {job['jobId']} {parameters}")
                logging.info(f"Submitted AWS Batch job {job['jobId']} with query {q}")
            else:
                logging.debug(f'dry-run: would have submitted {jname} with query {q}')

        if not self.dry_run:
            # upload AWS-Batch job ids to results bucket for better search
            # status checking
            self.upload_job_ids()


    def get_job_ids(self) -> List[str]:
        """Get a list of batch job ids"""
        # we can only query for job ids by jobs states which can change
        # between calls, so order in which job states are processed matters
        ids = defaultdict(int)        
        logging.debug(f'Retrieving job IDs from job queue {self.job_queue_name}')
        for status in AWS_BATCH_JOB_STATES:
            batch_of_jobs = self.batch.list_jobs(jobQueue=self.job_queue_name,
                                             jobStatus=status)
            for j in batch_of_jobs['jobSummaryList']:
                ids[j['jobId']] = 1

            while 'nextToken' in batch_of_jobs:
                batch_of_jobs = self.batch.list_jobs(jobQueue=self.job_queue_name,
                                                     jobStatus=status,
                                                     nextToken=batch_of_jobs['nextToken'])
                for j in batch_of_jobs['jobSummaryList']:
                    ids[j['jobId']] = 1

        logging.debug(f'Retrieved {len(ids.keys())} job IDs')
        return list(ids.keys())


    def upload_job_ids(self) -> None:
        """Save batch job ids in a metadata file in S3"""
        bucket_name, key = parse_bucket_name_key(f'{self.results_bucket}/{ELB_METADATA_DIR}/{ELB_AWS_JOB_IDS}')
        bucket = self.s3.Bucket(bucket_name)
        bucket.put_object(Body=json.dumps(self.job_ids).encode(), Key=key)


    def upload_query_length(self, query_length: int) -> None:
        """Save query length in a metadata file in S3"""
        if self.dry_run: return
        bucket_name, key = parse_bucket_name_key(f'{self.results_bucket}/{ELB_METADATA_DIR}/{ELB_AWS_QUERY_LENGTH}')
        bucket = self.s3.Bucket(bucket_name)
        bucket.put_object(Body=str(query_length).encode(), Key=key)


    def check_status(self) -> Dict[str, int]:
        """Report status of batch searches.

        Returns:
            Dictionary with counts of jobs in pending, running, succeeded, and
            failed status.
        """
        try:
            return self._check_status()
        except ClientError as e:
            if e.response.get('Error', {}).get('Code', 'Unknown') == 'AccessDenied':
                code = PERMISSIONS_ERROR
            else:
                code = CLUSTER_ERROR
            raise UserReportError(code, str(e))
        except ParamValidationError:
            raise UserReportError(CLUSTER_ERROR, "Cluster is not valid or not created yet")

    def _load_job_ids_from_aws(self):
        """ Retrieve the list of AWS Batch job IDs from AWS 
            First it tries to get them from S3, if this isn't available, gets this data from
            AWS Batch APIs.
            Post-condition: self.job_ids contains the list of job IDs for this search
        """
        with NamedTemporaryFile() as tmp:
            bucket_name, key = parse_bucket_name_key(os.path.join(self.results_bucket, ELB_METADATA_DIR, ELB_AWS_JOB_IDS))
            bucket = self.s3.Bucket(bucket_name)
            try:
                bucket.download_file(key, tmp.name)
                with open(tmp.name) as f_ids:
                    self.job_ids = json.load(f_ids)
            except ClientError as err:
                # if the metadata file does not exist, get job ids
                # and save them in metadata
                logging.debug(f'Failed to retrieve {os.path.join(self.results_bucket, ELB_METADATA_DIR, ELB_AWS_JOB_IDS)}')
                if err.response['Error']['Code'] == '404':
                    self.job_ids = self.get_job_ids()
                    self.upload_job_ids()
                else:
                    raise

    def _check_status(self) -> Dict[str, int]:
        """ Internal check_status, not protected against exceptions in AWS """
        if not self.dry_run:
            if not self.job_ids:
                self._load_job_ids_from_aws()

            counts : Dict[str, int] = defaultdict(int)
            # check status of jobs in batches of JOB_BATCH_NUM
            JOB_BATCH_NUM = 100
            for i in range(0, len(self.job_ids), JOB_BATCH_NUM):
                job_batch = self.batch.describe_jobs(jobs=self.job_ids[i:i + JOB_BATCH_NUM])['jobs']
                # get number for AWS Batch job states
                for st in AWS_BATCH_JOB_STATES:
                    counts[st] += sum([j['status'] == st for j in job_batch])

            # compute numbers for elastic-blast job states
            status = {
                'pending': counts['SUBMITTED'] + counts['PENDING'] + counts['RUNNABLE'] + counts['STARTING'],
                'running':  counts['RUNNING'],
                'succeeded': counts['SUCCEEDED'],
                'failed': counts['FAILED'],
            }
        else:
            status = defaultdict(int)
            logging.info('dry-run: would have checked status')
        return status
            
    def wait_until_done(self):
        if self.dry_run:
            return

        status = self.check_status()
        njobs = sum(status.values())
        ndone = 0
        logging.debug(f'Got {njobs} AWS jobs')

        done = False
        while not done:
            if ndone == njobs:
                done = True
                logging.debug(f'Done waiting for job')
            else:
                ndone = status['succeeded'] + status['failed']
                nrunning = status['running']
                logging.debug(f'njobs={njobs} nrunning={nrunning} ndone={ndone}')
                time.sleep(3)
                status = self.check_status()


    def _remove_ancillary_data(self, bucket_prefix: str) -> None:
        """ Removes ancillary data from the end user's result bucket
        bucket_prefix: path that follows the users' bucket name (looks like a file system directory)
        """
        bname, _ = parse_bucket_name_key(self.results_bucket)
        if not self.dry_run:
            s3_bucket = self.s3.Bucket(bname)
            s3_bucket.objects.filter(Prefix=bucket_prefix).delete()
        else:
            logging.debug(f'dry-run: would have removed {bname}/{bucket_prefix}')

    def _get_cloudformation_errors(self) -> List[str]:
        """Iterate over cloudformation stack events and extract error messages
        for failed resource creation or deletion. Cloudformation stack object
        must already be initialized.
        """
        # cloudformation stack must be initialized
        assert self.cf_stack
        messages = []
        for event in self.cf_stack.events.all():
            if event.resource_status == 'CREATE_FAILED' or \
                    event.resource_status == 'DELETE_FAILED':
                # resource creation may be canceled because other resources
                # were not created, these are not useful for reporting
                # problems
                if 'Resource creation cancelled' not in event.resource_status_reason:
                    messages.append(f'{event.logical_resource_id}: {event.resource_status_reason}')
        return messages

    def __str__(self):
        """ Print details about stack passed in as an argument, for debugging """
        st = self.cf_stack
        retval = f'Stack id: {st.stack_id}'
        retval += f'Stack name: {st.stack_name}'
        retval += f'Stack description: {st.description}'
        retval += f'Stack creation-time: {st.creation_time}'
        retval += f'Stack last-update: {st.last_updated_time}'
        retval += f'Stack status: {st.stack_status}'
        retval += f'Stack status reason: {st.stack_status_reason}'
        retval += f'Stack outputs: {st.outputs}'
        return retval