#   Copyright 2021 Dynatrace LLC
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import json
import os
import time
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import List, Dict

import azure.functions as func
from dateutil import parser

from . import logging
from .dynatrace_client import send_logs
from .filtering import LogFilter
from .mapping import extract_resource_id_attributes, extract_severity, azure_properties_names
from .metadata_engine import MetadataEngine
from .monitored_entity_id import infer_monitored_entity_id
from .self_monitoring import SelfMonitoring
from .utils import get_int_environment_value

record_age_limit = get_int_environment_value("DYNATRACE_LOG_INGEST_MAX_RECORD_AGE", 3600 * 24)
attribute_value_length_limit = get_int_environment_value("DYNATRACE_LOG_INGEST_ATTRIBUTE_VALUE_MAX_LENGTH", 250)
content_length_limit = get_int_environment_value("DYNATRACE_LOG_INGEST_CONTENT_MAX_LENGTH", 8192)
cloud_log_forwarder = os.environ.get("RESOURCE_ID", "")  # Function app id

DYNATRACE_URL = "DYNATRACE_URL"
DYNATRACE_ACCESS_KEY = "DYNATRACE_ACCESS_KEY"
DYNATRACE_LOG_INGEST_CONTENT_MARK_TRIMMED = "[TRUNCATED]"

metadata_engine = MetadataEngine()
log_filter = LogFilter()


def main(events: List[func.EventHubEvent]):
    self_monitoring = SelfMonitoring(execution_time=datetime.utcnow())
    process_logs(events, self_monitoring)


def process_logs(events: List[func.EventHubEvent], self_monitoring: SelfMonitoring):
    try:
        if DYNATRACE_URL not in os.environ.keys() or DYNATRACE_ACCESS_KEY not in os.environ.keys():
            raise Exception(f"Please set {DYNATRACE_URL} and {DYNATRACE_ACCESS_KEY} in application settings")

        logging.log_call_count = dict()
        dt_payload = []
        start_time = time.perf_counter()
        for event in events:
            timestamp = event.enqueued_time.replace(microsecond=0).replace(tzinfo=None).isoformat() + 'Z' if event.enqueued_time else None
            if not is_too_old(timestamp, self_monitoring, "event"):
                event_body = event.get_body()
                event_json = json.loads(event_body.decode('utf-8'))
                records = event_json.get("records", [])
                for record in records:
                    try:
                        process_record(dt_payload, record, self_monitoring)
                    except JSONDecodeError as json_e:
                        self_monitoring.parsing_errors += 1
                        logging.exception(f"Failed to decode JSON for the record:\n{record}\nThe reason:\n{json_e}",
                                              "log-record-parsing-jsondecode-exception")
                    except Exception as e:
                        self_monitoring.parsing_errors += 1
                        logging.exception(f"Failed to parse log record\n{record}.\nThe reason:\n{e}", "log-record-parsing-exception")

        self_monitoring.processing_time = time.perf_counter() - start_time
        logging.info(f"Successfully parsed {len(dt_payload)} log records")
        if dt_payload:
            send_logs(os.environ[DYNATRACE_URL], os.environ[DYNATRACE_ACCESS_KEY], dt_payload, self_monitoring)
    except Exception as e:
        logging.exception("Failed to process logs", "log-processing-exception")
        raise e
    finally:
        self_monitoring_enabled = os.environ.get("SELF_MONITORING_ENABLED", "False") in ["True", "true"]
        self_monitoring.log_self_monitoring_data()
        if self_monitoring_enabled:
            self_monitoring.push_time_series_to_azure()


def process_record(dt_payload: List[Dict], record: Dict, self_monitoring: SelfMonitoring):
    deserialize_properties(record)
    parsed_record = parse_record(record, self_monitoring)
    if parsed_record:
        timestamp = parsed_record.get("timestamp", None)
        if is_too_old(timestamp, self_monitoring, "record"):
            return
        dt_payload.append(parsed_record)


def is_too_old(timestamp: str, self_monitoring: SelfMonitoring, log_part: str):
    if timestamp:
        try:
            date = parser.parse(timestamp)
            # LINT won't accept any log line older than one day, 60 seconds of margin to send
            if (datetime.now(timezone.utc) - date).total_seconds() > (record_age_limit - 60):
                logging.info(f"Skipping too old {log_part} with timestamp '{timestamp}'")
                self_monitoring.too_old_records += 1
                return True
        except Exception:
            # Not much we can do when we can't parse the timestamp
            logging.exception(f"Failed to parse timestamp {timestamp}", "timestamp-parsing-exception")
            self_monitoring.parsing_errors += 1
    return False


def deserialize_properties(record: Dict):
    properties_name = next((properties for properties in azure_properties_names if properties in record.keys()), "")
    properties = record.get(properties_name, {})
    if properties and isinstance(properties, str):
        record["properties"] = json.loads(properties)


def parse_record(record: Dict, self_monitoring: SelfMonitoring):
    parsed_record = {
        "cloud.provider": "Azure"
    }
    extract_severity(record, parsed_record)
    extract_cloud_log_forwarder(parsed_record)

    if "resourceId" in record:
        resource_id = record.get("resourceId")
        extract_resource_id_attributes(parsed_record, resource_id)
    elif "_ResourceId" in record: #LogAnaltics schema https://docs.microsoft.com/en-us/azure/azure-monitor/logs/log-standard-columns
        resource_id = record.get("_ResourceId")
        extract_resource_id_attributes(parsed_record, resource_id)


    if log_filter.should_filter_out_record(parsed_record):
        return None

    metadata_engine.apply(record, parsed_record)
    category = record.get("category", "").lower()
    infer_monitored_entity_id(category, parsed_record)

    for attribute_key, attribute_value in parsed_record.items():
        if attribute_key not in ["content", "severity", "timestamp"] and attribute_value:
            string_attribute_value = attribute_value
            if not isinstance(attribute_value, str):
                string_attribute_value = str(attribute_value)
            parsed_record[attribute_key] = string_attribute_value[: attribute_value_length_limit]

    content = parsed_record.get("content", None)
    if content:
        if not isinstance(content, str):
            parsed_record["content"] = json.dumps(parsed_record["content"])
        if len(parsed_record["content"]) > content_length_limit:
            self_monitoring.too_long_content_size.append(len(parsed_record["content"]))
            trimmed_len = content_length_limit - len(DYNATRACE_LOG_INGEST_CONTENT_MARK_TRIMMED)
            parsed_record["content"] = parsed_record["content"][
                                       :trimmed_len] + DYNATRACE_LOG_INGEST_CONTENT_MARK_TRIMMED
    return parsed_record


def extract_cloud_log_forwarder(parsed_record):
    if cloud_log_forwarder:
        parsed_record["cloud.log_forwarder"] = cloud_log_forwarder
