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
Help functions to access and manipulate Kubernetes clusters

"""

import os
import json
import logging
import pathlib
import time
from tenacity import retry, stop_after_delay, stop_after_attempt, wait_random
from timeit import default_timer as timer
from pkg_resources import resource_string, resource_filename, set_extraction_path
from tempfile import TemporaryDirectory
from typing import List, Optional

from .util import safe_exec, gcp_get_blastdb_latest_path, ElbSupportedPrograms, SafeExecError
from .subst import substitute_params
from .constants import ELB_PAUSE_AFTER_INIT_PV, ELB_DOCKER_IMAGE
from .constants import K8S_JOB_BLAST, K8S_JOB_GET_BLASTDB
from .constants import K8S_JOB_IMPORT_QUERY_BATCHES, K8S_JOB_LOAD_BLASTDB_INTO_RAM, K8S_JOB_RESULTS_EXPORT
from .constants import ELB_K8S_JOB_SUBMISSION_MAX_WAIT
from .constants import ELB_K8S_JOB_SUBMISSION_MIN_WAIT
from .constants import ELB_K8S_JOB_SUBMISSION_MAX_RETRIES
from .constants import ELB_K8S_JOB_SUBMISSION_TIMEOUT, ELB_METADATA_DIR
from .constants import K8S_MAX_JOBS_PER_DIR, ELB_STATE_DISK_ID_FILE, ELB_QUERY_BATCH_DIR
from .filehelper import upload_file_to_gcs
from .elb_config import ElasticBlastConfig

def get_maximum_number_of_allowed_k8s_jobs(dry_run: bool = False) -> int:
    """ Returns the maximum number of kubernetes jobs """
    retval = 5000
    JSON_PATH = r"'{.spec.hard.count/jobs\.batch}'"
    cmd = f'kubectl get resourcequota gke-resource-quotas -o=jsonpath={JSON_PATH}'
    if not dry_run:
        try:
            p = safe_exec(cmd)
            if p.stdout:
                # the kubectl call returns a single value as a quoted string
                # (ex. '"5k"'), we take the substring [1:-1] to remove the quotes
                output = p.stdout.decode('utf-8').strip()[1:-1]
                if output.endswith('k'):  # Sample output: 5k
                    retval = int(output[:-1]) * 1000
                else:
                    retval = int(output)
        # cmd fails unless there is a k8s cluster elastic-blast is connected
        # to, ignore this error in that case and return the limit specified in
        # the GCP documentation: https://cloud.google.com/kubernetes-engine/quotas
        except SafeExecError:
            pass
    logging.debug(f"Limit on the number of kubernetes jobs: {retval}")
    return retval


def get_persistent_volumes() -> List[str]:
    """Return a list of persistent volume ids for a kubernetes cluster.
    Kubeconfig file determines the cluster that will be contacted.

    Raises:
        util.SafeExecError on problems communicating with the cluster
        RuntimeError when kubectl result cannot be parsed"""
    cmd = 'kubectl get pv -o json'
    p = safe_exec(cmd)
    try:
        dvols = json.loads(p.stdout.decode())
    except Exception as err:
        raise RuntimeError('Error when parsing listing of Kubernetes persistent volumes ' + str(err))
    if dvols is None:
        raise RuntimeError('Result of kubectl pv listing could not be read properly')
    return [i['metadata']['name'] for i in dvols['items']]


def get_persistent_disks(dry_run: bool = False) -> List[str]:
    """Return a list of persistent disks for a kubernetes cluster.
    Kubeconfig file determines the cluster that will be contacted.

    Raises:
        util.SafeExecError on problems communicating with the cluster
        json.decoder.JSONDecodeError on problems with parsing kubectl json output"""
    cmd = 'kubectl get pv -o json'
    if dry_run:
        logging.info(cmd)
    else:
        p = safe_exec(cmd)
        if p.stdout:
            pds = json.loads(p.stdout.decode())
            return [i['spec']['gcePersistentDisk']['pdName'] for i in pds['items']]
    return list()


@retry( stop=(stop_after_delay(ELB_K8S_JOB_SUBMISSION_TIMEOUT) | stop_after_attempt(ELB_K8S_JOB_SUBMISSION_MAX_RETRIES)), wait=wait_random(min=ELB_K8S_JOB_SUBMISSION_MIN_WAIT, max=ELB_K8S_JOB_SUBMISSION_MAX_WAIT))
def submit_jobs_with_retries(path: pathlib.Path, dry_run=False) -> List[str]:
    """ Retry kubernetes job submissions with the parameters specified in the decorator """
    return submit_jobs(path, dry_run)


def submit_jobs(path: pathlib.Path, dry_run=False) -> List[str]:
    """Submit kubernetes jobs using yaml files in the provided path.

    Arguments:
        path: Path to kubernetes job file or directory containing job files

    Returns:
        A list of submitted job names

    Raises:
        util.SafeExecError on problems with command line kubectl
        RuntimeError is path does not exist or provided directory is empty"""
    retval = list()
    if not path.exists():
        raise RuntimeError(f'Path with kubernetes jobs "{path}" does not exist')
    if path.is_dir():
        num_files = len(os.listdir(str(path)))
        if num_files == 0:
            raise RuntimeError(f'Job directory {str(path)} is empty')
        elif num_files > K8S_MAX_JOBS_PER_DIR:
            for f in sorted(os.listdir(str(path))):
                retval += submit_jobs_with_retries(pathlib.Path(os.path.join(path, f)), dry_run)
            return retval

    cmd = f'kubectl apply -f {path} -o json'
    if dry_run:
        logging.info(cmd)
    else:
        p = safe_exec(cmd)
        if p.stdout:
            out = json.loads(p.stdout.decode())
            if 'items' in out:
                retval = [i['metadata']['name'] for i in out['items']]
            else:
                retval = [out['metadata']['name']]
    return retval


def delete_all(dry_run: bool = False) -> List[str]:
    """Delete all kubernetes jobs, persitent volume claims, and persitent volumes.

    Returns:
        A list of deleted kubernetes objects

    Raises:
        util.SafeExecError on problems with command line kubectl"""
    cmd = 'kubectl delete jobs,pvc,pv --all'
    result = []
    if dry_run:
        logging.info(cmd)
    else:
        p = safe_exec(cmd)
        if p.stdout:
            for line in p.stdout.decode().split('\n'):
                if line:
                    # nothing was deleted
                    if line.startswith('No resources found'):
                        break
                    fields = line.split()
                    result.append(fields[1])
    return result


def get_jobs(selector: Optional[str] = None, dry_run: bool = False) -> List[str]:
    """Return a list of kubernetes jobs

    Arguments:
        selector: Kubernetes job label to select jobs
        dry_run: Dry run

    Raises:
        util.SafeExecError on problems with command line kubectl
        RuntimeError for unexpected kubctl output"""
    cmd = 'kubectl get jobs -o json'
    if selector is not None:
        cmd += f' -l {selector}'
    if dry_run:
        logging.info(cmd)
        return list()

    p = safe_exec(cmd)
    if not p.stdout:
        # a small JSON structure is always returned, even if there are no jobs
        raise RuntimeError('Unexpected lack of output for listing kubernetes jobs')
    out = json.loads(p.stdout.decode())
    return [i['metadata']['name'] for i in out['items']]


def _wait_for_job(job_file: pathlib.Path, attempts: int = 30, secs2wait: int = 60, dry_run: bool = False) -> None:
    """ Wait for the job to return successfully or raise a TimeoutError after specified number of attempts """

    for counter in range(attempts):
        if _job_succeeded(job_file, dry_run):
            break
        time.sleep(secs2wait)
    else:
        raise TimeoutError(f'{job_file} timed out')


def _job_succeeded(k8s_job_file: pathlib.Path, dry_run: bool = False) -> bool:
    """ Checks whether the job file passed in as an argument has succeeded or not.
    Returns true if the job succeeded, false otherwise.
    If the job failed, a RuntimeError is raised.
    """
    if not k8s_job_file.exists():
        raise FileNotFoundError(str(k8s_job_file))

    cmd = f'kubectl get -f {k8s_job_file} -o json'

    if dry_run:
        logging.info(cmd)
        return True

    p = safe_exec(cmd)
    if not p.stdout:
        return False

    retval = 0
    if not p.stdout:
        return False

    json_output = json.loads(p.stdout.decode())
    if 'status' not in json_output:
        return False

    final_status = ''
    if 'conditions' in json_output['status'] and len(json_output['status']['conditions']) > 0:
        final_status = json_output['status']['conditions'][0]['type']

    if final_status == 'Complete' and 'succeeded' in json_output['status']:
        retval = json_output['status']['succeeded']
    elif final_status == 'Failed' and 'failed' in json_output['status']:
        n = int(json_output['status']['failed'])
        logging.error(f'Job {k8s_job_file} failed {n} time(s)')
        raise RuntimeError(f'Job {k8s_job_file} failed {n} time(s)')
    return int(retval) == 1


def _ensure_successful_job(k8s_job_file: pathlib.Path, dry_run: bool = False) -> None:
    """ Verify that the k8s job succeeded
    Pre-condition: the string passed represents an existing k8s file
    Raises a RuntimeException if job failed
    """
    if not k8s_job_file.exists():
        raise FileNotFoundError(str(k8s_job_file))

    cmd = f'kubectl get -f {k8s_job_file} -o json'

    if dry_run:
        logging.info(cmd)
        return

    p = safe_exec(cmd)
    status = json.loads(p.stdout.decode())['status']['succeeded']
    if int(status) != 1:
        raise RuntimeError(f'{k8s_job_file} failed: {p.stderr.decode()}')


def initialize_storage(cfg: ElasticBlastConfig, db: str, db_path: str = '') -> None:
    """ Initialize storage for ElasticBLAST cluster """
    use_local_ssd = cfg.cluster.use_local_ssd
    if use_local_ssd:
        initialize_local_ssd(cfg, db, db_path)
    else:
        initialize_persistent_disk(cfg, db, db_path)
        label_persistent_disk(cfg)


def initialize_local_ssd(cfg: ElasticBlastConfig, db: str, db_path: str = '') -> None:
    """ Initialize local SSDs for ElasticBLAST cluster """
    if not db:
        raise ValueError("Argument 'db' can't be empty")
    dry_run = cfg.cluster.dry_run
    init_blastdb_minutes_timeout = cfg.timeouts.init_pv
    num_nodes = cfg.cluster.num_nodes
    program = cfg.blast.program
    job_init_template = 'job-init-local-ssd.yaml.template'
    taxdb_path = ''
    if db_path:
        # Custom database
        job_init_template = 'job-init-local-ssd-custom.yaml.template'
        taxdb_path = gcp_get_blastdb_latest_path() + '/taxdb.*'
    subs = {
        'ELB_DB': db,
        'ELB_DB_PATH': db_path,
        'ELB_TAX_DB_PATH': taxdb_path,
        'ELB_DB_MOL_TYPE': ElbSupportedPrograms().get_molecule_type(program),
        'ELB_BLASTDB_SRC': cfg.blast.db_source.name,
        'NODE_ORDINAL': '0',
        'ELB_DOCKER_IMAGE': ELB_DOCKER_IMAGE,
        'K8S_JOB_GET_BLASTDB' : K8S_JOB_GET_BLASTDB,
        'K8S_JOB_LOAD_BLASTDB_INTO_RAM' : K8S_JOB_LOAD_BLASTDB_INTO_RAM,
        'K8S_JOB_IMPORT_QUERY_BATCHES' : K8S_JOB_IMPORT_QUERY_BATCHES,
        'K8S_JOB_BLAST' : K8S_JOB_BLAST,
        'K8S_JOB_RESULTS_EXPORT' : K8S_JOB_RESULTS_EXPORT
    }
    with TemporaryDirectory() as d:
        set_extraction_path(d)

        start = timer()
        job_init_local_ssd_tmpl = resource_string('elastic_blast', f'templates/{job_init_template}').decode()
        for n in range(num_nodes):
            job_init_local_ssd = pathlib.Path(os.path.join(d, f'job-init-local-ssd-{n}.yaml'))
            subs['NODE_ORDINAL'] = str(n)
            with job_init_local_ssd.open(mode='wt') as f:
                f.write(substitute_params(job_init_local_ssd_tmpl, subs))
        cmd = f"kubectl apply -f {d}"
        if dry_run:
            logging.info(cmd)
        else:
            safe_exec(cmd)

        # wait for multiple jobs
        timeout = init_blastdb_minutes_timeout * 60
        sec2wait = 20
        while timeout > 0:
            cmd = f'kubectl get jobs -o jsonpath=' \
                '{.items[?(@.status.active)].metadata.name}{\'\\t\'}' \
                '{.items[?(@.status.failed)].metadata.name}{\'\\t\'}' \
                '{.items[?(@.status.succeeded)].metadata.name}'
            if dry_run:
                logging.info(cmd)
                res = '\t\t' + \
                    ' '.join([f'init-ssd-{n}' for n in range(num_nodes)])
            else:
                proc = safe_exec(cmd)
                res = proc.stdout.decode()
                logging.debug(res)
            active, failed, succeeded = res.split('\t')
            if failed:
                proc = safe_exec(f'kubectl logs -l app=setup')
                for line in proc.stdout.split('\n'):
                    logging.debug(line)
                raise RuntimeError(f'Local SSD initialization jobs failed: {failed}')
            if not active:
                logging.debug(f'Local SSD initialization jobs succeeded: {succeeded}')
                break
            time.sleep(sec2wait)
            timeout -= sec2wait
        if timeout < 0:
            raise TimeoutError(f'{d} jobs timed out')
        end = timer()
        logging.debug(f'RUNTIME init-storage {end-start} seconds')
        # Delete setup jobs
        if not 'ELB_DONT_DELETE_SETUP_JOBS' in os.environ:
            cmd = 'kubectl delete jobs -l app=setup'
            if dry_run:
                logging.info(cmd)
            else:
                safe_exec(cmd)


def initialize_persistent_disk(cfg: ElasticBlastConfig, db: str, db_path: str = '') -> None:
    """ Initialize Persistent Disk for ElasticBLAST execution
    Arguments:
        cfg - configuration to get parameters from
        db - BLAST db name
        db_path - if custom database, path to the database files
    """

    # ${LOGDATETIME} setup_pd start >>${ELB_LOGFILE}
    if not db:
        raise ValueError("Argument 'db' can't be empty")
    cluster_name = cfg.cluster.name
    pd_size = str(cfg.cluster.pd_size)
    program = cfg.blast.program
    job_init_pv_template = 'job-init-pv.yaml.template'
    taxdb_path = ''
    if db_path:
        # Custom database
        job_init_pv_template = 'job-init-pv-custom.yaml.template'
        taxdb_path = gcp_get_blastdb_latest_path() + '/taxdb.*'

    results_bucket = cfg.cluster.results
    dry_run = cfg.cluster.dry_run
    query_batches = os.path.join(results_bucket, ELB_QUERY_BATCH_DIR)

    init_blastdb_minutes_timeout = cfg.timeouts.init_pv

    subs = {
        'QUERY_BATCHES': query_batches,
        'ELB_PD_SIZE': pd_size,
        'ELB_CLUSTER_NAME': cluster_name,
        'ELB_DB': db,
        'ELB_DB_PATH': db_path,
        'ELB_TAX_DB_PATH': taxdb_path,
        'ELB_DB_MOL_TYPE': ElbSupportedPrograms().get_molecule_type(program),
        'ELB_BLASTDB_SRC': cfg.blast.db_source.name,
        'BUCKET': results_bucket,
        'ELB_DOCKER_IMAGE': ELB_DOCKER_IMAGE,
        'ELB_TAXIDLIST'     : cfg.blast.taxidlist if cfg.blast.taxidlist is not None else '',
        'K8S_JOB_GET_BLASTDB' : K8S_JOB_GET_BLASTDB,
        'K8S_JOB_LOAD_BLASTDB_INTO_RAM' : K8S_JOB_LOAD_BLASTDB_INTO_RAM,
        'K8S_JOB_IMPORT_QUERY_BATCHES' : K8S_JOB_IMPORT_QUERY_BATCHES,
        'K8S_JOB_BLAST' : K8S_JOB_BLAST,
        'K8S_JOB_RESULTS_EXPORT' : K8S_JOB_RESULTS_EXPORT
    }

    with TemporaryDirectory() as d:
        set_extraction_path(d)
        storage_gcp = resource_filename('elastic_blast', 'templates/storage-gcp-ssd.yaml')
        cmd = f"kubectl apply -f {storage_gcp}"
        if dry_run:
            logging.info(cmd)
        else:
            safe_exec(cmd)

        pvc_yaml = os.path.join(d, 'pvc.yaml')
        with open(pvc_yaml, 'wt') as f:
            f.write(substitute_params(resource_string('elastic_blast', 'templates/pvc.yaml.template').decode(), subs))
        cmd = f"kubectl apply -f {pvc_yaml}"
        if dry_run:
            logging.info(cmd)
        else:
            safe_exec(cmd)

        start = timer()
        job_init_pv = pathlib.Path(os.path.join(d, 'job-init-pv.yaml'))
        with job_init_pv.open(mode='wt') as f:
            f.write(substitute_params(resource_string('elastic_blast', f'templates/{job_init_pv_template}').decode(), subs))
        cmd = f"kubectl apply -f {job_init_pv}"
        if dry_run:
            logging.info(cmd)
        else:
            safe_exec(cmd)

        _wait_for_job(job_init_pv, init_blastdb_minutes_timeout,
                      dry_run=dry_run)
        end = timer()
        logging.debug(f'RUNTIME init-pv {end-start} seconds')

        # save persistent disk id so that it can be deleted on clean up
        disk = get_persistent_disks(dry_run)
        if disk:
            cfg.appstate.disk_id = disk[0]
            disk_id_file = os.path.join(d, ELB_STATE_DISK_ID_FILE)
            with open(disk_id_file, 'w') as f:
                print(cfg.appstate.disk_id, file=f)
            dest = os.path.join(cfg.cluster.results, ELB_METADATA_DIR, ELB_STATE_DISK_ID_FILE)
            upload_file_to_gcs(disk_id_file, dest, dry_run)

        if not 'ELB_DONT_DELETE_SETUP_JOBS' in os.environ:
            cmd = f"kubectl delete -f {job_init_pv}"
            if dry_run:
                logging.info(cmd)
            else:
                get_logs('app=setup', [K8S_JOB_GET_BLASTDB,
                                       K8S_JOB_IMPORT_QUERY_BATCHES], dry_run)
                safe_exec(cmd)
        # ${LOGDATETIME} setup_pd end >>${ELB_LOGFILE}

        # Interim fix to prevent mouont errors on BLAST k8s jobs (EB-239?, EB-282?)
        if not dry_run:
            secs2sleep = int(os.getenv('ELB_PAUSE_AFTER_INIT_PV', str(ELB_PAUSE_AFTER_INIT_PV)))
            time.sleep(secs2sleep)


def label_persistent_disk(cfg: ElasticBlastConfig) -> None:
    use_local_ssd = cfg.cluster.use_local_ssd
    if use_local_ssd:
        return
    dry_run = cfg.cluster.dry_run
    cluster_name = cfg.cluster.name
    # Label disk with given claim with standard labels
    pv_claim = 'blast-dbs-pvc'
    labels = cfg.cluster.labels
    get_pv_cmd = 'kubectl get pv -o custom-columns=CLAIM:.spec.claimRef.name,PDNAME:.spec.gcePersistentDisk.pdName'
    if dry_run:
        logging.info(get_pv_cmd)
        pd_name = f'disk_name_with_claim_{pv_claim}'
    else:
        proc = safe_exec(get_pv_cmd)
        output = proc.stdout.decode()
        pd_name = ''
        for line in output.split('\n'):
            parts = line.split()
            if len(parts) < 2:
                continue
            if parts[0] == pv_claim:
                pd_name = parts[1]
        if not pd_name:
            raise LookupError(f"Disk with claim '{pv_claim}' can't be found in cluster '{cluster_name}'")
    zone = cfg.gcp.zone
    cmd = f'gcloud compute disks update {pd_name} --update-labels {labels} --zone {zone} --project {cfg.gcp.project}'
    if dry_run:
        logging.info(cmd)
    else:
        safe_exec(cmd)


def check_server(dry_run: bool = False):
    """Check that server set after gcp.get_gke_credentials is alive"""
    cmd = 'kubectl version --short'
    if dry_run:
        logging.info(cmd)
    else:
        safe_exec(cmd)


def get_logs(label: str, containers: List[str], dry_run: bool = False):
    """ Collect logs from Kubernetes.
      Parameters:
        label - Kubernetes label to specify log source
        containers - list of Kubernetes containers to get logs from
        dry_run - report command only, don't execute it.
    """
    for c in containers:
        cmd = f'kubectl logs -l {label} -c {c} --timestamps --since=24h --tail=-1'
        if dry_run:
            logging.info(cmd)
        else:
            try:
                # kubectl logs command can fail if the pod/container is gone, so we suppress error. We can't combine it into one try-except-finally, because safe_exec should report the command used in DEBUG level using old format with timestamps. New bare format is used only after successful invocation of kubectl logs.
                proc = safe_exec(cmd)
                try:
                    # Temporarily modify format for logging because we import true timestamps
                    # from Kubernetes and don't need logging timestamps, so we just copy logs
                    # verbatim.
                    root_logger = logging.getLogger()
                    orig_formatter = root_logger.handlers[0].formatter
                    root_logger.handlers[0].setFormatter(logging.Formatter(fmt='%(message)s'))
                    for line in proc.stdout.decode().split('\n'):
                        if line:
                            logging.info(line)
                finally:
                    # Ensure logging is restored to previous format
                    # type is ignored because orig_formatter can be None
                    # and there does not seem to be any other way to get
                    # the original formatter from root logger
                    root_logger.handlers[0].setFormatter(orig_formatter) # type: ignore
            except SafeExecError:
                pass