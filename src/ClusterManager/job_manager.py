#!/usr/bin/env python3

import json
import os
import time
import argparse
import sys
import datetime
import collections
import yaml
import base64
import copy
import logging
import logging.config

from prometheus_client import Histogram
import redis

from cluster_manager import setup_exporter_thread, manager_iteration_histogram, register_stack_trace_dump, update_file_modification_time, record

from job_launcher import PythonLauncher, LauncherStub
import joblog_manager
from job_launcher import get_job_status_detail, job_status_detail_with_finished_time

sys.path.append(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "../utils"))

from DataHandler import DataHandler
from config import config, GetStoragePath
import notify
import k8sUtils
from cluster_resource import ClusterResource
from job_params_util import get_resource_params_from_job_params
from common import base64decode, base64encode, walk_json

logger = logging.getLogger(__name__)

job_state_change_histogram = Histogram(
    "job_state_change_latency_seconds",
    """latency for job to change state(seconds).
        Possible value for current_state is approved/scheduling/running.
        It means how much time it takes for a job change state from previous state
        to current state. The order of state:
        created -> approved -> scheduling -> running.
        For example, approved current_state means how much time it takes for a job
        to change state from created to approved, running current_state means how
        long it takes from scheduling to running.""",
    buckets=(1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0,
             float("inf")),
    labelnames=("current_state",))


class JobTimeRecord(object):
    def __init__(self,
                 create_time=None,
                 approve_time=None,
                 submit_time=None,
                 running_time=None):
        self.create_time = create_time
        self.approve_time = approve_time
        self.submit_time = submit_time
        self.running_time = running_time

    @staticmethod
    def parse_time(t):
        if t is None:
            return None
        return datetime.datetime.fromtimestamp(t)

    @staticmethod
    def to_timestamp(t):
        if t is None:
            return None
        return time.mktime(t.timetuple())

    @staticmethod
    def parse(m):
        c_time = JobTimeRecord.parse_time(m.get("create_time"))
        a_time = JobTimeRecord.parse_time(m.get("approve_time"))
        s_time = JobTimeRecord.parse_time(m.get("submit_time"))
        r_time = JobTimeRecord.parse_time(m.get("running_time"))
        return JobTimeRecord(c_time, a_time, s_time, r_time)

    def to_map(self):
        return {
            "create_time": JobTimeRecord.to_timestamp(self.create_time),
            "approve_time": JobTimeRecord.to_timestamp(self.approve_time),
            "submit_time": JobTimeRecord.to_timestamp(self.submit_time),
            "running_time": JobTimeRecord.to_timestamp(self.running_time),
        }


def b64encode(str_val):
    return base64.b64encode(str_val.encode("utf-8")).decode("utf-8")


def b64decode(str_val):
    return base64.b64decode(str_val.encode("utf-8")).decode("utf-8")


def to_job_status_key(job_id):
    return "job_status_" + job_id


def load_job_status(redis_conn, job_id):
    try:
        val = redis_conn.get(to_job_status_key(job_id))
        if val is not None:
            val = val.decode("utf-8")
            return JobTimeRecord.parse(json.loads(val))
    except Exception:
        logger.exception("load job status failed")
    return JobTimeRecord()


def set_job_status(redis_conn, job_id, job_status):
    try:
        val = json.dumps(job_status.to_map())
        redis_conn.set(to_job_status_key(job_id), val)
    except Exception:
        logger.exception("set job status failed")


# If previous state has no record, which means the job_manager get restarted
# or previous entry is expired, we ignore this entry.


def update_job_state_latency(redis_conn, job_id, state, event_time=None):
    if event_time is None:
        event_time = datetime.datetime.utcnow()

    job_status = load_job_status(redis_conn, job_id)
    changed = False

    if state == "created":
        if job_status.create_time is None:
            changed = True
            job_status.create_time = event_time
    elif state == "approved":
        if job_status.approve_time is None:
            changed = True
            job_status.approve_time = event_time
        if changed and job_status.create_time is not None:
            changed = True
            elapsed = (event_time - job_status.create_time).seconds
            job_state_change_histogram.labels(state).observe(elapsed)
    elif state == "scheduling":
        if job_status.submit_time is None:
            changed = True
            job_status.submit_time = event_time
        if changed and job_status.approve_time is not None:
            changed = True
            elapsed = (event_time - job_status.approve_time).seconds
            job_state_change_histogram.labels(state).observe(elapsed)
    elif state == "running":
        if job_status.running_time is None:
            changed = True
            job_status.running_time = event_time
        # because UpdateJobStatus will call update_job_state_latency
        # multiple times, so here need to avoid override metric
        if changed and job_status.submit_time is not None:
            changed = True
            elapsed = (event_time - job_status.submit_time).seconds
            job_state_change_histogram.labels(state).observe(elapsed)

    if changed:
        set_job_status(redis_conn, job_id, job_status)


def get_job_total_gpu(job_params):
    num_workers = 1
    if "numpsworker" in job_params:
        num_workers = int(job_params["numpsworker"])
    return int(job_params["resourcegpu"]) * num_workers


@record
def ApproveJob(redis_conn, job, dataHandlerOri=None):
    try:
        job_id = job["jobId"]
        vcName = job["vcName"]

        update_job_state_latency(redis_conn,
                                 job_id,
                                 "created",
                                 event_time=job["jobTime"])

        jobParams = json.loads(b64decode(job["jobParams"]))
        job_total_gpus = get_job_total_gpu(jobParams)

        if dataHandlerOri is None:
            dataHandler = DataHandler()
        else:
            dataHandler = dataHandlerOri

        if "preemptionAllowed" in jobParams and jobParams[
                "preemptionAllowed"] is True:
            logger.info("Job %s preemptible, approve!", job_id)
            detail = [{
                "message": "waiting for available preemptible resource."
            }]

            dataFields = {
                "jobStatusDetail": b64encode(json.dumps(detail)),
                "jobStatus": "queued",
                "lastUpdated": datetime.datetime.now().isoformat(),
            }
            conditionFields = {"jobId": job_id}
            dataHandler.UpdateJobTextFields(conditionFields, dataFields)
            update_job_state_latency(redis_conn, job_id, "approved")
            if dataHandlerOri is None:
                dataHandler.Close()
            return True

        vcList = dataHandler.ListVCs()
        vc = None
        for item in vcList:
            if item["vcName"] == vcName:
                vc = item
                break
        if vc is None:
            logger.warning("Vc not exising! job {}, vc {}".format(
                job_id, vcName))
            if dataHandlerOri is None:
                dataHandler.Close()
            return False
        metadata = json.loads(vc["metadata"])

        if "user_quota" in metadata:
            user_running_jobs = dataHandler.GetJobList(
                job["userName"],
                vcName,
                status="running,queued,scheduling",
                op=("=", "or"))
            running_gpus = 0
            for running_job in user_running_jobs:
                running_jobParams = json.loads(
                    b64decode(running_job["jobParams"]))
                # ignore preemptible GPUs
                if "preemptionAllowed" in running_jobParams and running_jobParams[
                        "preemptionAllowed"] is True:
                    continue
                running_job_total_gpus = get_job_total_gpu(running_jobParams)
                running_gpus += running_job_total_gpus

            logger.info(
                "Job %s require %s, used quota (exclude preemptible GPUs) %s, with user quota of %s.",
                job_id, job_total_gpus, running_gpus, metadata["user_quota"])
            if job_total_gpus > 0 and int(
                    metadata["user_quota"]) < (running_gpus + job_total_gpus):
                logger.info(
                    "Job %s excesses the user quota: %s + %s > %s. Will need approve from admin.",
                    job_id, running_gpus, job_total_gpus,
                    metadata["user_quota"])
                detail = [{
                    "message":
                        "exceeds the user quota in VC: {} (used) + {} (requested) > {} (user quota). Will need admin approval."
                        .format(running_gpus, job_total_gpus,
                                metadata["user_quota"])
                }]
                dataHandler.UpdateJobTextFields(
                    {"jobId": job["jobId"]},
                    {"jobStatusDetail": b64encode(json.dumps(detail))})
                if dataHandlerOri is None:
                    dataHandler.Close()
                return False

        detail = [{"message": "waiting for available resource."}]

        dataFields = {
            "jobStatusDetail": b64encode(json.dumps(detail)),
            "jobStatus": "queued",
            "lastUpdated": datetime.datetime.now().isoformat(),
        }
        conditionFields = {"jobId": job_id}
        dataHandler.UpdateJobTextFields(conditionFields, dataFields)
        update_job_state_latency(redis_conn, job_id, "approved")
        if dataHandlerOri is None:
            dataHandler.Close()
        return True
    except Exception as e:
        logger.warning(e, exc_info=True)
    finally:
        if dataHandlerOri is None:
            dataHandler.Close()


UnusualJobs = {}


@record
def UpdateJobStatus(redis_conn,
                    launcher,
                    job,
                    notifier=None,
                    dataHandlerOri=None):
    assert (job["jobStatus"] == "scheduling" or job["jobStatus"] == "running")
    if dataHandlerOri is None:
        dataHandler = DataHandler()
    else:
        dataHandler = dataHandlerOri
    jobParams = json.loads(b64decode(job["jobParams"]))

    result, details, diagnostics = launcher.get_job_status(job["jobId"])
    logger.info("Job status: %s %s", job["jobId"], result)

    jobPath, workPath, dataPath = GetStoragePath(jobParams["jobPath"],
                                                 jobParams["workPath"],
                                                 jobParams["dataPath"])
    localJobPath = os.path.join(config["storage-mount-path"], jobPath)
    logPath = os.path.join(localJobPath, "logs/joblog.txt")

    if "userId" not in jobParams:
        jobParams["userId"] = "0"

    if result == "Succeeded":
        joblog_manager.extract_job_log(job["jobId"], logPath,
                                       jobParams["userId"])

        # TODO: Refactor
        detail = get_job_status_detail(job)
        detail = job_status_detail_with_finished_time(detail, "finished")

        dataFields = {
            "jobStatusDetail": b64encode(json.dumps(detail)),
            "jobStatus": "finished",
            "lastUpdated": datetime.datetime.now().isoformat(),
        }
        conditionFields = {"jobId": job["jobId"]}
        dataHandler.UpdateJobTextFields(conditionFields, dataFields)

        launcher.delete_job(job["jobId"], force=True)

        if notifier is not None:
            notifier.notify(
                notify.new_job_state_change_message(job["userName"],
                                                    job["jobId"],
                                                    result.strip()))
    elif result == "Running":
        update_job_state_latency(redis_conn, job["jobId"], "running")
        if job["jobStatus"] != "running":
            started_at = k8sUtils.localize_time(datetime.datetime.now())
            detail = [{
                "startedAt": started_at,
                "message": "started at: {}".format(started_at)
            }]

            last_updated = datetime.datetime.now()

            dataFields = {
                "jobStatusDetail": b64encode(json.dumps(detail)),
                "jobStatus": "running",
                "lastUpdated": last_updated.isoformat(),
            }
            conditionFields = {"jobId": job["jobId"]}
            dataHandler.UpdateJobTextFields(conditionFields, dataFields)
            if notifier is not None:
                notifier.notify(
                    notify.new_job_state_change_message(job["userName"],
                                                        job["jobId"],
                                                        result.strip()))
            job["lastUpdated"] = last_updated

        params = json.loads(base64decode(job["jobParams"]))
        max_time = params.get("maxTimeSec")
        if type(max_time) != int:
            if max_time is not None:
                logger.info("unknown maxTimeSec %s for job %s", max_time,
                            job["jobId"])
        else:
            max_time = int(max_time)
            start_time = int(datetime.datetime.timestamp(job["lastUpdated"]))
            now = datetime.datetime.timestamp(datetime.datetime.now())
            if start_time + max_time < now:
                logger.info(
                    "killing job %s for its running time exceed maxTimeSec %ss, start %s, now %s",
                    job["jobId"], max_time, start_time, now)
                error_msg = "running exceed pre-defined %ss" % (max_time)
                dataFields = {
                    "errorMsg": error_msg,
                }
                conditionFields = {"jobId": job["jobId"]}
                dataHandler.UpdateJobTextFields(conditionFields, dataFields)
                launcher.kill_job(job["jobId"], "killed")
                if notifier is not None:
                    notifier.notify(
                        notify.new_job_killed_message(job["userName"],
                                                      job["jobId"], error_msg))

    elif result == "Failed":
        now = datetime.datetime.now()
        params = json.loads(base64decode(job["jobParams"]))
        if params.get("debug") is True and (now - job["jobTime"]).seconds < 60:
            logger.info("leave job %s there for debug for 60s", job["jobId"])
            return
        logger.warning("Job %s fails, cleaning...", job["jobId"])

        if notifier is not None:
            notifier.notify(
                notify.new_job_state_change_message(job["userName"],
                                                    job["jobId"],
                                                    result.strip()))

        joblog_manager.extract_job_log(job["jobId"], logPath,
                                       jobParams["userId"])

        # TODO: Refactor
        detail = get_job_status_detail(job)
        detail = job_status_detail_with_finished_time(detail, "failed")

        dataFields = {
            "jobStatusDetail": b64encode(json.dumps(detail)),
            "jobStatus": "failed",
            "errorMsg": diagnostics,
            "lastUpdated": datetime.datetime.now().isoformat(),
        }
        conditionFields = {"jobId": job["jobId"]}
        dataHandler.UpdateJobTextFields(conditionFields, dataFields)

        launcher.delete_job(job["jobId"], force=True)
    elif result == "Unknown" or result == "NotFound":
        if job["jobId"] not in UnusualJobs:
            logger.warning("!!! Job status ---{}---, job: {}".format(
                result, job["jobId"]))
            UnusualJobs[job["jobId"]] = datetime.datetime.now()
        # TODO
        # 1) May need to reduce the timeout.
        #     It takes minutes before pod turns into "Unknown", we may don't need to wait so long.
        # 2) If node resume before we resubmit the job, the job will end in status 'NotFound'.
        elif (datetime.datetime.now() - UnusualJobs[job["jobId"]]).seconds > 30:
            del UnusualJobs[job["jobId"]]

            # TODO refine later
            # before resubmit the job, reset the endpoints
            # update all endpoint to status 'pending', so it would restart when job is ready
            endpoints = dataHandler.GetJobEndpoints(job["jobId"])
            for endpoint_id, endpoint in list(endpoints.items()):
                endpoint["status"] = "pending"
                logger.debug("Reset endpoint status to 'pending': {}".format(
                    endpoint_id))
                dataHandler.UpdateEndpoint(endpoint)

            logger.warning(
                "Job {} fails in Kubernetes as {}, delete and re-submit.".
                format(job["jobId"], result))
            launcher.kill_job(job["jobId"], "queued", update_queue_time=False)
            if notifier is not None:
                notifier.notify(
                    notify.new_job_state_change_message(job["userName"],
                                                        job["jobId"],
                                                        result.strip()))

    elif result == "Pending":
        _, detail = k8sUtils.GetJobStatus(job["jobId"])
        dataHandler.UpdateJobTextFields({"jobId": job["jobId"]}, {
            "jobStatusDetail": b64encode(json.dumps(detail)),
            "jobStatus": "scheduling",
        })

    if result != "Unknown" and result != "NotFound" and job[
            "jobId"] in UnusualJobs:
        del UnusualJobs[job["jobId"]]
    if dataHandlerOri is None:
        dataHandler.Close()


def create_log(logdir="/var/log/dlworkspace", process_name="jobmanager"):
    if not os.path.exists(logdir):
        os.system("mkdir -p " + logdir)
    with open('logging.yaml') as f:
        logging_config = yaml.full_load(f)
        f.close()
        logging_config["handlers"]["file"]["filename"] = "%s/%s.log" % (
            logdir, process_name)
        logging.config.dictConfig(logging_config)


def get_priority_dict():
    try:
        dataHandler = DataHandler()
        priority_dict = dataHandler.get_job_priority()
        return priority_dict
    except Exception as e:
        logger.warning("Fetch job priority dict failed!", exc_info=True)
        return {}
    finally:
        dataHandler.Close()


def get_job_priority(priority_dict, job_id):
    if job_id in list(priority_dict.keys()):
        return priority_dict[job_id]
    return 100


def discount_cluster_resource(cluster_resource):
    # Hard-code 95% of the total capacity is application usable
    # TODO: Find a better way to manage system and user resource quota
    cluster_resource.cpu *= 0.95
    cluster_resource.memory *= 0.95
    return cluster_resource


def get_cluster_schedulable(cluster_status):
    # Compute cluster schedulable resource
    cluster_capacity = ClusterResource(
        params={
            "cpu": cluster_status["cpu_capacity"],
            "memory": cluster_status["memory_capacity"],
            "gpu": cluster_status["gpu_capacity"],
        })
    # On 1 node, reserved = unschedulable - used
    cluster_reserved = ClusterResource(
        params={
            "cpu": cluster_status["cpu_reserved"],
            "memory": cluster_status["memory_reserved"],
            "gpu": cluster_status["gpu_reserved"],
        })

    cluster_schedulable = cluster_capacity - cluster_reserved
    cluster_schedulable = discount_cluster_resource(cluster_schedulable)
    logger.info("cluster schedulable: %s", cluster_schedulable)
    return cluster_schedulable


def get_vc_schedulables(cluster_status):
    # Compute VC schedulable resources
    vc_statuses = cluster_status.get("vc_statuses", {})
    vc_schedulables = {}
    for vc_name, vc_status in vc_statuses.items():
        vc_capacity = ClusterResource(
            params={
                "cpu": vc_status["cpu_capacity"],
                "memory": vc_status["memory_capacity"],
                "gpu": vc_status["gpu_capacity"],
            })
        vc_unschedulable = ClusterResource(
            params={
                "cpu": vc_status["cpu_unschedulable"],
                "memory": vc_status["memory_unschedulable"],
                "gpu": vc_status["gpu_unschedulable"],
            })
        vc_schedulable = vc_capacity - vc_unschedulable
        vc_schedulables[vc_name] = discount_cluster_resource(vc_schedulable)

    logger.info("vc schedulables: %s", vc_schedulables)
    return vc_schedulables


def get_jobs_info(jobs, cluster_schedulable, vc_schedulables):
    priority_dict = get_priority_dict()

    jobs_info = []
    for job in jobs:
        job_status = job.get("jobStatus")
        if job_status in ["queued", "scheduling", "running"]:
            allowed = False
            allowed_resource = None

            job_params = json.loads(base64decode(job["jobParams"]))
            preemption_allowed = job_params.get("preemptionAllowed", False)
            job_id = job_params["jobId"]
            job_training_type = job_params["jobtrainingtype"]

            job_res = get_resource_params_from_job_params(job_params)
            job_resource = ClusterResource(params=job_res)
            job_preemptable_resource = None
            if "preemptable_resource" in job_res:
                job_preemptable_resource = ClusterResource(params=job_res["preemptable_resource"])

            vc_name = job["vcName"]
            vc_schedulable = vc_schedulables.get(vc_name)
            if vc_schedulable is None:
                logger.warning(
                    "vc %s does not exist as provided by %s, ignore this job",
                    vc_name, job_id)
                continue
            if (not preemption_allowed or job_training_type == "InferenceJob") and job_status in [
                    "scheduling", "running"
            ]:
                # do not preempt non preemptable jobs, and non-preemptable part of inference jobs
                # do not skip inference job since we need to schedule preempt part later
                vc_schedulable -= job_resource
                cluster_schedulable -= job_resource
                if not preemption_allowed:
                    continue
                else:
                    allowed = True
                    allowed_resource = copy.deepcopy(job_resource)

            # Job lists will be sorted based on and in the order of below
            # 1. non-preemptible precedes preemptible
            # 2. training job precedes inference job (to minimize GPU fragmentation when submitting job)
            # 3. running precedes scheduling, precedes queued
            # 4. larger priority value precedes lower priority value
            # 5. early job time precedes later job time

            # Non-Preemptible jobs first
            preemptible = 1 if preemption_allowed else 0

            # job type
            inference = 1 if job_training_type == "InferenceJob" or job_training_type == "CPUInferenceJob" else 0

            # Job status
            job_status_key = 0
            if job["jobStatus"] == "scheduling":
                job_status_key = 1
            elif job["jobStatus"] == "queued":
                job_status_key = 2

            # Priority value
            reverse_priority = get_job_priority(priority_dict, job_id)
            priority = 999999 - reverse_priority

            # Job time
            queue_time = int(datetime.datetime.timestamp(job["lastUpdated"]))

            sort_key = "{}_{}_{}_{:06d}_{}".format(preemptible, inference, job_status_key,
                                                priority, queue_time)

            single_job_info = {
                "job": job,
                "preemptionAllowed": preemption_allowed,
                "jobId": job_id,
                "jobtrainingtype": job_training_type,
                "job_resource": job_resource,
                "job_preemptable_resource": job_preemptable_resource,
                "sort_key": sort_key,
                "allowed": allowed,
                "allowed_resource": allowed_resource,
                "status": job_status,
                "reason": None,
            }

            jobs_info.append(single_job_info)

    jobs_info.sort(key=lambda x: x["sort_key"])
    return jobs_info


def mark_schedulable_non_preemptable_jobs(jobs_info, cluster_schedulable,
                                          vc_schedulables,
                                          vc_scheduling_policies):
    stop_schedulings = {} # vc_name -> the first blocking job of this vc

    for job_info in jobs_info:
        job_resource = job_info["job_resource"]
        job_id = job_info["jobId"]

        logger.info("Job %s : %s : %s", job_id, job_resource,
                    job_info["sort_key"])

        vc_name = job_info["job"]["vcName"]
        vc_schedulable = vc_schedulables.get(vc_name)
        if vc_schedulable is None:
            logger.warning(
                "vc %s is not exist as provided by %s, ignore this job",
                vc_name, job_id)
            continue

        preemption_allowed = job_info.get("preemptionAllowed", False)
        if preemption_allowed or job_info["jobtrainingtype"] == "InferenceJob":
            continue # schedule non preemptable first

        scheduling_policy = vc_scheduling_policies.get(vc_name, "RF")

        if scheduling_policy == "FIFO":
            if stop_schedulings.get(vc_name) is None and \
                    cluster_schedulable >= job_resource and \
                    vc_schedulable >= job_resource:
                vc_schedulable -= job_resource
                cluster_schedulable -= job_resource
                job_info["allowed"] = True
                logger.info(
                    "Allow non-preemptable job %s from %s to run, job resource %s, policy %s",
                    job_id, vc_name, job_resource, scheduling_policy)
            elif stop_schedulings.get(vc_name) is None:
                reason = "resource not enough, required %s, vc schedulable %s, cluster schedulable %s" % (
                    job_resource, vc_schedulable, cluster_schedulable)
                job_info["reason"] = reason
                logger.info(
                    "Disallow non-preemptable job %s from vc %s to run."
                    "resource not enough, required %s. "
                    "cluster schedulable %s, vc schedulables %s, policy %s",
                    job_id, vc_name, job_resource, cluster_schedulable,
                    vc_schedulable, scheduling_policy)
                # prevent later non preemptable job from scheduling
                stop_schedulings[vc_name] = job_info
            else:
                reason = "blocked by job with higher priority/earlier time %s" % (
                    stop_schedulings[vc_name]['jobId'])
                job_info["reason"] = reason
                logger.info(
                    "Disallow non-preemptable job %s from vc %s to run."
                    "job with higher priority is disallowed %s", job_id,
                    vc_name, stop_schedulings[vc_name]["jobId"])
        else:
            if scheduling_policy != "RF":
                logger.error("unknown scheduling_policy %s, default to RF",
                             scheduling_policy)
            if cluster_schedulable >= job_resource and \
                    vc_schedulable >= job_resource:
                vc_schedulable -= job_resource
                cluster_schedulable -= job_resource
                job_info["allowed"] = True
                logger.info(
                    "Allow non-preemptable job %s from %s to run, job resource %s, policy %s",
                    job_id, vc_name, job_resource, scheduling_policy)
            else:
                logger.info(
                    "Disallow non-preemptable job %s from vc %s to run."
                    "Requiring %s, vc_schedulable %s, cluster_schedulable %s, policy %s",
                    job_id, vc_name, job_resource, vc_schedulable,
                    cluster_schedulable, scheduling_policy)

def mark_schedulable_preemptable_jobs(jobs_info, cluster_schedulable):
    for job_info in jobs_info:
        if job_info["jobtrainingtype"] == "InferenceJob":
            continue

        preemption_allowed = job_info.get("preemptionAllowed", False)
        if preemption_allowed and (job_info["allowed"] is False):
            job_resource = job_info["job_resource"]
            job_id = job_info["jobId"]
            if cluster_schedulable >= job_resource:
                logger.info(
                    "Allow preemptable job %s to run. "
                    "cluster schedulable %s. "
                    "used job resource %s.", job_id, cluster_schedulable,
                    job_resource)
                # Strict FIFO policy not required for global (bonus) tokens
                # since these jobs are anyway preemptable.
                cluster_schedulable -= job_resource
                job_info["allowed"] = True
            else:
                logger.info(
                    "Disallow preemptable job %s to run, "
                    "insufficient cluster resource: "
                    "cluster schedulable %s, "
                    "required job resource %s.", job_id, cluster_schedulable,
                    job_resource)

# schedule non-preempt part of queued inference jobs
def mark_schedulable_inference_jobs_non_preemptable_part(jobs_info, cluster_schedulable, vc_schedulables):
    for job_info in jobs_info:
        if job_info["jobtrainingtype"] != "InferenceJob" or job_info["status"] != "queued":
            continue

        job_resource = job_info["job_resource"]
        job_id = job_info["jobId"]
        vc_name = job_info["job"]["vcName"]
        vc_schedulable = vc_schedulables.get(vc_name)
        if vc_schedulable is None:
            logger.warning(
                "vc %s is not exist as provided by %s, ignore this job",
                vc_name, job_id)
            continue

        if cluster_schedulable >= job_resource and \
                vc_schedulable >= job_resource:
            vc_schedulable -= job_resource
            cluster_schedulable -= job_resource
            job_info["allowed"] = True
            job_info["allowed_resource"] = copy.deepcopy(job_resource)
            logger.info(
                "Allow inference job %s non-preemptable part from %s to run, job resource %s",
                job_id, vc_name, job_resource)
        else:
            logger.info(
                "Disallow inference job %s non-preemptable part from vc %s to run."
                "Requiring %s, vc_schedulable %s, cluster_schedulable %s",
                job_id, vc_name, job_resource, vc_schedulable,
                cluster_schedulable)

# schedule preempt part of running/scheduling/queued inference jobs
def mark_schedulable_inference_jobs_preemptable_part(jobs_info, cluster_schedulable):
    for job_info in jobs_info:
        # skip training job, or
        # inference job which non-preemptable part is not allowed or has no preemptable part
        if job_info["jobtrainingtype"] != "InferenceJob" or \
            job_info["allowed"] is False or \
            job_info["job_preemptable_resource"] is None:
            continue

        job_preemptable_resource = job_info["job_preemptable_resource"]
        job_id = job_info["jobId"]

        schedulable_resource = None
        if cluster_schedulable >= job_preemptable_resource:
            schedulable_resource = job_preemptable_resource
        else:
            # If cluster resource is insufficient, allocate remaining resource to job in proportion
            # Assume remaining GPU is less than CPU, memory.
            job_gpu_key = list(job_preemptable_resource.gpu.to_dict())[0]
            job_gpu_request = list(job_preemptable_resource.gpu.to_dict().values())[0]
            job_cpu_request = job_preemptable_resource.cpu.to_dict()[job_gpu_key]
            job_memory_request = job_preemptable_resource.memory.to_dict()[job_gpu_key]
            cluster_gpu_count = cluster_schedulable.gpu.to_dict()[job_gpu_key]

            schedulable_gpu = min(job_gpu_request, cluster_gpu_count)
            schedulable_cpu = schedulable_gpu * job_cpu_request / job_gpu_request
            schedulable_memory = schedulable_gpu * job_memory_request / job_gpu_request

            schedulable_resource = ClusterResource(
                params={
                    "cpu": {job_gpu_key: schedulable_cpu},
                    "memory": {job_gpu_key: schedulable_memory},
                    "gpu": {job_gpu_key: schedulable_gpu}
                }
            )

        if schedulable_resource.has_empty_gpu_or_cpu():
            logger.info(
                "Disallow inference job %s preemptable part to run, "
                "cluster schedulable %s. "
                "job preemptable resource %s. "
                "schedulable resource %s.", job_id, cluster_schedulable,
                job_preemptable_resource, schedulable_resource)
        else:
            logger.info(
                "Allow inference job %s preemptable part to run. "
                "cluster schedulable %s. "
                "job preemptable resource %s."
                "schedulable resource %s.", job_id, cluster_schedulable,
                job_preemptable_resource, schedulable_resource)

            job_info["allowed"] = True
            cluster_schedulable -= schedulable_resource
            job_info["allowed_resource"] += schedulable_resource


def adjust_job_resource(data_handler, job_info):
    job = job_info["job"]
    if job_info["allowed_resource"] is not None:
        params = json.loads(base64decode(job["jobParams"]))
        gpu_list = list(job_info["allowed_resource"].gpu.to_dict().values())
        params["resourcegpu"] = int(gpu_list[0]) if len(gpu_list) > 0 else 0
        job["jobParams"] = base64encode(json.dumps(params))
        if data_handler is not None:
            data_handler.UpdateJobTextFields(
                {"jobId": job_info["jobId"]},
                {"jobParams": job["jobParams"]})
        logger.info("inference job %s gpu count after adjust is %d", job_info["jobId"], params["resourcegpu"])

    return job

def schedule_jobs(jobs_info, data_handler, redis_conn, launcher,
                  cluster_schedulable, vc_schedulables):
    for job_info in jobs_info:
        try:
            job = job_info["job"]
            job_id = job_info["jobId"]
            job_training_type = job_info["jobtrainingtype"]
            job_resource = job_info["job_resource"]
            vc_name = job["vcName"]
            job_status = job["jobStatus"]
            preemption_allowed = job_info.get("preemptionAllowed", False)
            allowed = job_info["allowed"]
            sort_key = job_info["sort_key"]

            if job_training_type == "InferenceJob":
                job = adjust_job_resource(data_handler, job_info)

            if job_status == "queued" and allowed:
                launcher.submit_job(job)
                update_job_state_latency(redis_conn, job_id, "scheduling")
                logger.info("Submitting job %s : %s", job_id, sort_key)
            elif preemption_allowed and job_training_type != "InferenceJob" and \
                    (job_status in ["scheduling", "running"]) and (not allowed):
                launcher.kill_job(job_id, "queued", update_queue_time=False)
                logger.info("Preempting job %s : %s", job_id, sort_key)
            elif job_status == "queued" and (not allowed):
                vc_schedulable = vc_schedulables[vc_name]
                if job_info["reason"] is not None:
                    message = job_info["reason"]
                else:
                    message = "Waiting for resource. Job request %s. " \
                              "VC schedulable %s. Cluster schedulable %s" % \
                              (job_resource, vc_schedulable, cluster_schedulable)
                detail = [{"message": message}]
                data_handler.UpdateJobTextFields(
                    {"jobId": job_id},
                    {"jobStatusDetail": base64encode(json.dumps(detail))})
            elif job_training_type == "InferenceJob" and (job_status in ["scheduling", "running"]):
                launcher.scale_job(job)
        except Exception as e:
            logger.error("Process job failed: %s, %s", job_info, e, exc_info=True)


@record
def take_job_actions(data_handler, redis_conn, launcher, jobs):
    # Compute from the latest ClusterStatus in DB:
    # 1. cluster_schedulable
    # 2. vc_schedulables
    cluster_status, _ = data_handler.GetClusterStatus()
    cluster_schedulable = get_cluster_schedulable(cluster_status)
    vc_schedulables = get_vc_schedulables(cluster_status)

    vcs = data_handler.ListVCs()
    vc_scheduling_policies = {} # vc_name -> policy
    for vc in vcs:
        # Support following scheduling_policy:
        # * FIFO: This policy allows big job blocks small jobs, might cause low GPU utils
        # * RF: Runnable first: sort according to submission time and priority, submit jobs not exceeding quota
        metadata = json.loads(vc["metadata"])
        vc_scheduling_policies[vc["vcName"]] = walk_json(metadata,
                                                         "admin",
                                                         "scheduling_policy",
                                                         default="RF")

    # Parse and sort jobs based on priority and submission time
    jobs_info = get_jobs_info(jobs, cluster_schedulable, vc_schedulables)

    # Mark schedulable non-preemptable training jobs
    mark_schedulable_non_preemptable_jobs(jobs_info, cluster_schedulable,
                                          vc_schedulables,
                                          vc_scheduling_policies)

    # Mark schedulable inference jobs non-preemptable part
    mark_schedulable_inference_jobs_non_preemptable_part(jobs_info,
                                                        cluster_schedulable,
                                                        vc_schedulables)
    # Mark schedulable preemptable training jobs
    mark_schedulable_preemptable_jobs(jobs_info, cluster_schedulable)

    # Mark schedulable inference jobs preemptable part
    mark_schedulable_inference_jobs_preemptable_part(jobs_info, cluster_schedulable)

    logger.info("cluster schedulable after this round of scheduling: %s",
                cluster_schedulable)

    # Submit/kill jobs based on schedulable marking
    schedule_jobs(jobs_info, data_handler, redis_conn, launcher,
                  cluster_schedulable, vc_schedulables)


def is_version_satisified(actual, base):
    actual = list(map(int, actual.split(".")))
    base = list(map(int, base.split(".")))

    i = 0
    for i in range(min(len(actual), len(base))):
        if actual[i] > base[i]:
            return True
        elif actual[i] < base[i]:
            return False
    return len(actual) >= len(base)


def Run(redis_port, target_status):
    register_stack_trace_dump()
    process_name = "job_manager_" + target_status

    create_log(process_name=process_name)

    notifier = notify.Notifier(config.get("job-manager"))
    notifier.start()

    kube_server_version = os.environ.get("KUBE_SERVER_VERSION", "0")
    # https://kubernetes.io/docs/concepts/configuration/pod-priority-preemption/#non-preempting-priority-class
    is_support_pod_priority = is_version_satisified(kube_server_version, "1.15")
    config["is_support_pod_priority"] = is_support_pod_priority
    logger.info("kube server version is %s, is_support_pod_priority %s",
                kube_server_version, is_support_pod_priority)

    launcher_type = config.get("job-manager", {}).get("launcher", "python")
    if launcher_type == "python":
        launcher = PythonLauncher()
    elif launcher_type == "controller":
        launcher = LauncherStub()
    else:
        logger.error("unknown launcher_type %s", launcher_type)
        sys.exit(2)
    launcher.start()

    redis_conn = redis.StrictRedis(host="localhost", port=redis_port, db=0)

    while True:
        update_file_modification_time(process_name)

        with manager_iteration_histogram.labels(process_name).time():
            try:
                config["racks"] = k8sUtils.get_node_labels("rack")
                config["skus"] = k8sUtils.get_node_labels("sku")
            except Exception as e:
                logger.exception("get node labels failed")

            try:
                launcher.wait_tasks_done(
                ) # wait for tasks from previous batch done

                data_handler = DataHandler()

                if target_status == "queued":
                    jobs = data_handler.GetJobList(
                        "all",
                        "all",
                        num=None,
                        status="queued,scheduling,running")
                    take_job_actions(data_handler, redis_conn, launcher, jobs)
                else:
                    jobs = data_handler.GetJobList("all",
                                                   "all",
                                                   num=None,
                                                   status=target_status)
                    logger.info("Updating status for %d %s jobs", len(jobs),
                                target_status)

                    for job in jobs:
                        logger.info("Processing job: %s, status: %s" %
                                    (job["jobId"], job["jobStatus"]))
                        if job["jobStatus"] == "killing":
                            launcher.kill_job(job["jobId"], "killed")
                        elif job["jobStatus"] == "pausing":
                            launcher.kill_job(job["jobId"], "paused")
                        elif job["jobStatus"] == "running":
                            UpdateJobStatus(redis_conn,
                                            launcher,
                                            job,
                                            notifier,
                                            dataHandlerOri=data_handler)
                        elif job["jobStatus"] == "scheduling":
                            UpdateJobStatus(redis_conn,
                                            launcher,
                                            job,
                                            notifier,
                                            dataHandlerOri=data_handler)
                        elif job["jobStatus"] == "unapproved":
                            ApproveJob(redis_conn,
                                       job,
                                       dataHandlerOri=data_handler)
                        else:
                            logger.error("unknown job status %s for job %s",
                                         job["jobStatus"], job["jobId"])
            except Exception as e:
                logger.exception("Process jobs failed!")
            finally:
                try:
                    data_handler.Close()
                except:
                    pass

        time.sleep(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis_port",
                        "-r",
                        help="port of redis",
                        type=int,
                        default=9300)
    parser.add_argument("--port",
                        "-p",
                        help="port of exporter",
                        type=int,
                        default=9200)
    parser.add_argument(
        "--status",
        "-s",
        help="target status to update, queued is a special status",
        type=str,
        default="queued")

    args = parser.parse_args()
    setup_exporter_thread(args.port)

    Run(args.redis_port, args.status)
