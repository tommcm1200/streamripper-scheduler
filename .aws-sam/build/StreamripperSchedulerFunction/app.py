import datetime
import os

import pytz
from pytz import timezone

import logging

import boto3
from boto3 import resource
from boto3.dynamodb.conditions import Key

import json

logger = logging.getLogger('tipper')
logger.setLevel(logging.INFO)
# logger.debug('debug message')
# logger.info('info message')
# logger.warn('warn message')
# logger.error('error message')
# logger.critical('critical message')

# Enviroment Variables
days = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
streamripper_queue_arn = os.environ['STREAMRIPPER_QUEUE_ARN']
table_name = os.environ['SCHEDULE_TABLE_NAME']
# radio_station_table_name = 'streamripper_radio-station-details'
radio_station_table_name = os.environ['RADIO_STATION_DETAILS_TABLE_NAME']

# The boto3 dynamoDB resource
dynamodb_resource = resource('dynamodb')


def next_weekday(d, weekday, start_time):
    days_ahead = days.index(weekday) - d.weekday()

    if days_ahead < 0: # Target day already happened this week
        days_ahead += 7

    return d + datetime.timedelta(days_ahead)

def calc_show_duration(next_show_start,next_show_finish):
    show_duration = next_show_finish - next_show_start
    return show_duration.seconds

def create_upate_cw_evet(show_info):

    events_client = boto3.client('events')

    cw_rule_name = "Streamripper_autogen-{0}-{1}-Rule".format(show_info["radio_station"],
                                                                    show_info["show_name"])
    # print("Creating events rule '{}': {}".format(cw_rule_name, show_info))
    logger.info("Creating events rule '{}': {}".format(cw_rule_name, show_info))

    show_info["show_start_utc_day"][0:3]

    cw_rule_schedule = "cron({0} {1} ? * {2} *)".format(show_info["show_start_utc_min"],
                                     show_info["show_start_utc_hour"],
                                     show_info["show_start_utc_day"][0:3]
                                     )

    target_input = { "url" : show_info["show_url"],
                     "showname": show_info["show_name"],
                     "radiostation":  show_info["radio_station"],
                     "duration": show_info["show_duration"]
                     }

    rule_response = events_client.put_rule(
        Name=cw_rule_name,
        ScheduleExpression=cw_rule_schedule,
        State='ENABLED',
    )

    events_client.put_targets(
        Rule=cw_rule_name,
        Targets=[
            {
                'Id': "1",
                'Arn': streamripper_queue_arn,
                'Input' : json.dumps(target_input)
            },
        ]
    )

def show_time_to_utc(show_info):
    # When is the next scheduled show in local timezone format
    d = datetime.date.today()
    next_showday = next_weekday(d, show_info['show_day'], show_info['show_start'])  # 0 = Monday, 1=Tuesday, 2=Wednesday...

    # Create datetime objects
    show_start_time = datetime.datetime.strptime(show_info['show_start'], '%H:%M').time()
    show_finish_time = datetime.datetime.strptime(show_info['show_finish'], '%H:%M').time()

    next_show_start = datetime.datetime(
        next_showday.year,
        next_showday.month,
        next_showday.day,
        show_start_time.hour,
        show_start_time.minute
    )

    next_show_finish = datetime.datetime(
        next_showday.year,
        next_showday.month,
        next_showday.day,
        show_finish_time.hour,
        show_finish_time.minute
    )

    next_show_start_tz = show_info['show_tz'].localize(next_show_start)
    logger.info('Next Scheduled {} show start time: {}'.format(show_info['show_name'],next_show_start_tz.strftime("%Y-%m-%d %H:%M:%S %Z%z")))

    # Convert to UTC time
    next_show_start_utc = next_show_start_tz.astimezone(timezone('UTC'))
    logger.info('Next Scheduled {} show start time in UTC: {}'.format(show_info['show_name'],next_show_start_utc.strftime("%Y-%m-%d %H:%M:%S %Z%z")))

    show_info["show_start_utc_day"] = days[next_show_start_utc.weekday()]
    show_info["show_start_utc_hour"] = next_show_start_utc.hour
    show_info["show_start_utc_min"] = next_show_start_utc.minute
    show_info["show_duration"] = calc_show_duration(next_show_start, next_show_finish)

    return show_info

def get_table_metadata(table_name):
    """
    Get some metadata about chosen table.
    """
    table = dynamodb_resource.Table(table_name)

    return {
        'num_items': table.item_count,
        'primary_key_name': table.key_schema[0],
        'status': table.table_status,
        'bytes_size': table.table_size_bytes,
        'global_secondary_indices': table.global_secondary_indexes
    }

def scan_table(table_name, filter_key=None, filter_value=None):
    """
    Perform a scan operation on table.
    Can specify filter_key (col name) and its value to be filtered.
    """
    table = dynamodb_resource.Table(table_name)

    if filter_key and filter_value:
        filtering_exp = Key(filter_key).eq(filter_value)
        response = table.scan(FilterExpression=filtering_exp)
    else:
        response = table.scan()

    return response

def query_table(table_name, filter_key=None, filter_value=None):
    """
    Perform a query operation on the table.
    Can specify filter_key (col name) and its value to be filtered.
    """
    table = dynamodb_resource.Table(table_name)

    if filter_key and filter_value:
        filtering_exp = Key(filter_key).eq(filter_value)
        response = table.query(KeyConditionExpression=filtering_exp)
    else:
        response = table.query()

    return response

def lambda_handler(event, context):

    # Scan Table
    logger.info("Table {} Scan for complete schedule".format(table_name))
    schedule_table_items = scan_table(table_name)['Items']

    # Create CW Event based on show info
    for item in schedule_table_items:

        show_info = {}
        show_info['radio_station'] = item['radio_station']
        show_info['show_name'] = item['show_name']
        # show_info['show_url'] = item['show_url']
        show_info['show_start'] = item['show_start']
        show_info['show_finish'] = item['show_finish']
        show_info['show_day'] = item['show_day'].upper()
        show_info['show_tz'] = timezone(item['show_tz'])

        # Get Show URL from radio_station table
        logger.info("Getting show_url from {} table ".format(radio_station_table_name))
        show_url_query_response = query_table(radio_station_table_name , 'radio_station' , show_info['radio_station'])
        show_info['show_url'] = show_url_query_response['Items'][0]['show_url']
        show_info['file_ext'] = show_url_query_response['Items'][0]['file_ext']

        # Convert show schedule to UTC format
        show_info = show_time_to_utc(show_info)

        # Create/Update CW Event for recording
        create_upate_cw_evet(show_info)