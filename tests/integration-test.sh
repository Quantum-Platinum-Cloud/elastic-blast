#!/bin/bash
# tests/integration-test.sh: End-to-end ElasticBLAST blast search
#
# Author: Christiam Camacho (camacho@ncbi.nlm.nih.gov)
# Created: Wed 06 May 2020 06:59:03 AM EDT

SCRIPT_DIR=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
set -euo pipefail

# All other settings are specified in the config file
CFG=${1:-"${SCRIPT_DIR}/../share/etc/elb-blastn-pdbnt.ini"}
ROOT_DIR=${SCRIPT_DIR}/..
QUERY_BATCHES=${ELB_RESULTS}/query_batches
export ELB_DONT_DELETE_SETUP_JOBS=1
export BLAST_USAGE_REPORT=false

DRY_RUN=''
#DRY_RUN=--dry-run     # uncomment for debugging
timeout_minutes=${2:-5}
logfile=elb.log
rm -f $logfile

# if set to "false", the script will not download search results
TEST_RESULTS=${3:-true}

get_num_cores() {
    retval=1
    if which parallel >&/dev/null; then
        retval=$(parallel --number-of-cores)
    elif [ -f /proc/cpuinfo ] ; then
        retval=$(grep -c '^proc' /proc/cpuinfo)
    elif which lscpu >& /dev/null; then
        retval=$(lscpu -p | grep -v ^# | wc -l)
    elif [ `uname -s` == 'Darwin' ]; then
        retval=$(sysctl -n hw.ncpu)
    fi
    echo $retval
}
NTHREADS=$(get_num_cores)

cleanup_resources_on_error() {
    set +e
    time $ROOT_DIR/elastic-blast delete --cfg $CFG --loglevel DEBUG --logfile $logfile $DRY_RUN
    exit 1;
}

TMP=`mktemp -t $(basename -s .sh $0)-XXXXXXX`
trap "cleanup_resources_on_error; /bin/rm -f $TMP" INT QUIT HUP KILL ALRM ERR

rm -fr *.fa *.out.gz elb-*.log
$ROOT_DIR/elastic-blast submit --cfg $CFG --loglevel DEBUG --logfile $logfile $DRY_RUN

attempts=0
[ ! -z "$DRY_RUN" ] || sleep 10    # Should be enough for the BLAST k8s jobs to get started

while [ $attempts -lt $timeout_minutes ]; do
    $ROOT_DIR/elastic-blast status --cfg $CFG $DRY_RUN | tee $TMP
    #set +e
    if grep '^Pending 0' $TMP && grep '^Running 0' $TMP; then
        break
    fi
    attempt=$((attempts+1))
    sleep 60
    #set -e
done

if [ $TEST_RESULTS = false ] ; then
    exit 0
fi

if ! grep -qi aws $CFG; then
    make logs 2>&1 | tee -a $logfile
    $ROOT_DIR/elastic-blast run-summary --cfg $CFG --loglevel DEBUG --logfile $logfile $DRY_RUN
    # Get intermediate results
    gsutil -qm cp ${QUERY_BATCHES}/*.fa .

    # Get results
    gsutil -qm cp ${ELB_RESULTS}/*.out.gz .
    gsutil -qm cp ${ELB_RESULTS}/metadata/* .

    $ROOT_DIR/elastic-blast delete --cfg $CFG --loglevel DEBUG --logfile $logfile $DRY_RUN

    # Test results
    find . -name "batch*.out.gz" -type f -print0 | \
        xargs -0 -P $NTHREADS  -I{} gzip -t {}
    if grep 'outfmt 11' $logfile; then
        find . -name "batch*.out.gz" -type f -print0 | \
            xargs -0 -P $NTHREADS -I{} \
            bash -c "zcat {} | datatool -m /netopt/ncbi_tools64/c++.metastable/src/objects/blast/blast.asn -M /am/ncbiapdata/asn/asn.all -v - -e /dev/null"
    fi
    test $(ls -1 *fa | wc -l) -eq $(ls -1 *.out.gz | wc -l)
    test $(du -a -b *.out.gz | sort -n | head -n 1 | cut -f 1) -gt 0
else
    $ROOT_DIR/elastic-blast delete --cfg $CFG --loglevel DEBUG --logfile $logfile $DRY_RUN
    # Get query batches
    aws s3 cp ${QUERY_BATCHES}/ . --recursive --exclude '*' --include "*.fa" --exclude '*/*'
    # As we have no logs yet we can't check ASN.1 integrity
    # Get results
    aws s3 cp ${ELB_RESULTS}/ . --recursive --exclude '*' --include "*.out.gz" --exclude '*/*'
    # Test results
    find . -name "batch*.out.gz" -type f -print0 | \
        xargs -0 -P $NTHREADS  -I{} gzip -t {}
    test $(du -a -b *.out.gz | sort -n | head -n 1 | cut -f 1) -gt 0
fi