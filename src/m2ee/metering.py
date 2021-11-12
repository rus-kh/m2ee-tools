#
# Copyright (C) 2021 Mendix. All rights reserved.
#
import string
import datetime
import hashlib
import shutil
import json
import logging
import re

import psycopg2
import requests
import socket

from os import path
from os import makedirs
from psycopg2 import sql
from psycopg2.extras import NamedTupleCursor
from time import mktime
from time import time
from datetime import datetime


logger = logging.getLogger(__name__)
usage_metrics_schema_version = "1.2"


def export_usage_metrics(m2ee):
    try:
        logger.info("Begin exporting usage metrics %s", datetime.now())

        # trying to get server_id at the very beginning to 
        # drop futher execution if Mendix app is not running
        # and server_id is unavailable
        server_id = m2ee.client.get_license_information()["license_id"]

        config = m2ee.config

        db_conn = open_db_connection(config)

        with db_conn:
            with prepare_db_cursor_for_usage_query(config, db_conn) as db_cursor:
                # starting export
                is_subscription_service_available = check_subscription_service_availability(config)
                if is_subscription_service_available:
                    # export to Subscription Service directly if it is available (for connected environments)
                    logger.info("Exporting to the Subscription Service")
                    export_to_subscription_service(config, db_cursor, server_id)
                else:
                    logger.info("Exporting to file")
                    # export to file (for disconnected environments)
                    export_to_file(config, db_cursor, server_id)
    except Exception as e:
        logger.error(e)
    finally:
        # context manager doesn't close the connection so we need to do this manually
        close_db_connection(db_conn)


def open_db_connection(config):
    pg_config = config.get_pg_environment() 
    db_name = pg_config['PGDATABASE']
    db_username = pg_config['PGUSER']
    db_password = pg_config['PGPASSWORD']

    conn = psycopg2.connect(
        database = db_name, 
        user = db_username, 
        password = db_password
    )

    logger.debug("Usage metering: connected to database")

    return conn


def close_db_connection(conn):
    conn.close()
    logger.debug("Usage metering: database is disconnected")


def prepare_db_cursor_for_usage_query(config, db_conn):
    logger.debug("Begin to prepare usage metering query %s", datetime.now())
    
    # the base query
    query = sql.SQL("""
        SELECT 
            u.name, 
            u.lastlogin, 
            u.webserviceuser, 
            u.blocked, 
            u.active, 
            u.isanonymous,
            ur.usertype
            {email} 
        FROM system$user u 
        LEFT JOIN system$userreportinfo_user ur_u ON u.id = ur_u.system$userid 
        LEFT JOIN system$userreportinfo ur ON ur.id = ur_u.system$userreportinfoid
        {extra_joins}
        WHERE u.name IS NOT NULL 
        ORDER BY u.id
    """)

    # looking for email entities
    email_table_and_columns = get_email_columns(config, db_conn)

    # preparing found email tables and columns to join the query as {email} and {extra_joins}
    email_part, joins_part = convert_found_user_attributes_to_query_additions(email_table_and_columns)

    # combining final query
    query = query.format(
        email = email_part,
        extra_joins = joins_part
    )

    logger.trace("Constructed query: %s", query.as_string(db_conn))
    
    # executing query via separate db cursor since we use batching here
    batch_cur = get_batch_db_cursor(config, db_conn)
    batch_cur.execute(query)

    logger.debug("Usage metering query is ready %s", datetime.now())

    return batch_cur


def check_subscription_service_availability(config):
    url = config.get_usage_metrics_subscription_service_uri()

    try:
        response = requests.post(url)

        if response.status_code == requests.codes.ok:
            return True
    except:
        pass
    
    return False


def export_to_subscription_service(config, db_cursor, server_id):
    usage_metrics = []

    # fetching query results in batches (psycopg does all the batching work implicitly)
    for usage_metric in db_cursor:
        metric_to_export = convert_data_for_export(usage_metric, server_id, db_cursor)
        usage_metrics.append(metric_to_export)

    # submitting data to Subscription Service API
    send_to_subscription_service(config, server_id, usage_metrics)


def send_to_subscription_service(config, server_id, usage_metrics):
    headers = {
        'Content-Type': 'application/json',
        'Schema-Version': usage_metrics_schema_version
    }

    body = {}
    body["subscriptionSecret"] = server_id
    body["environmentName"] = ""
    body["projectID"] = config.get_project_id()
    body["users"] = usage_metrics
    body = json.dumps(body)

    url = config.get_usage_metrics_subscription_service_uri()
    subscription_service_timeout = config.get_usage_metrics_subscription_service_timeout()

    logger.trace("Metering request headers: %s" % headers)
    logger.trace("Metering request body: %s" % body)

    try:
        response = requests.post(
            url=url, 
            data=body, 
            headers=headers, 
            timeout=subscription_service_timeout
        )
    except socket.timeout as e:
        message = "Subscription Service API does not respond. "
        "Timeout reached after %s seconds." % subscription_service_timeout
        logger.trace(message)
    except socket.error as e:
        message = "Subscription Service API not available for requests: (%s: %s)" % (type(e), e)
        logger.trace(message)

    response_body = response.content.decode('utf-8')

    logger.trace("Subscription Service response: %s" % response_body)

    if response.status_code != requests.codes.ok:
        logger.error(
            "Non OK http status code: %s %s" %
            (response.headers, response_body)
        )
        return
    
    response = json.loads(response_body)
    result = response['licensekey'] 
    
    if result:
        logger.info("Usage metrics exported %s to Subscription Service", datetime.now())    
    else:
        error_msg = response['logmessages'] if response['logmessages'] else ""
        logger.error("Subscription Service error %s" % error_msg)

def export_to_file(config, db_cursor, server_id):
    # create file
    file_suffix = str(int(time()))
    output_dir = create_output_dir(config, file_suffix)
    output_file = path.join(
            output_dir,
            config.get_usage_metrics_output_file_name() + "_" + file_suffix + ".json"
        )

    with open(output_file, "w") as out_file:
        # dump usage metering data to file
        i = 1
        out_file.write("[\n")
        for usage_metric in db_cursor:
            export_data = convert_data_for_export(usage_metric, server_id, db_cursor, True)
            # no comma before the first element in JSON array
            if i > 1:
                out_file.write(",\n")
            i += 1        
            # using json.dump() instead of json.dumps() and dump each row separately here 
            # to reduce memory usage since json.dumps() could consume a lot of memory for a 
            # large number of users (e.g. it takes ~3Gb for 1 million users) 
            json.dump(export_data, out_file, indent = 4, sort_keys = True)
        out_file.write("\n]")

        logger.info("Usage metrics exported %s to %s", datetime.now(), output_file)

    generate_md5(output_file)
    zip_files(output_dir, file_suffix)


def convert_data_for_export(usage_metric, server_id, db_cursor, to_file = False):
    converted_data = {}

    column_names = column_names = [desc[0] for desc in db_cursor.description]

    converted_data["active"] = usage_metric.active
    converted_data["blocked"] = usage_metric.blocked
    # prefer email from the name field over the email field
    # since email is mandatory field for the Subscription Service, 
    # setting email value as empty if there is no email user columns
    converted_data["emailDomain"] = get_hashed_email_domain(usage_metric.name, usage_metric.email) if 'email' in column_names else ""
    # isAnonymous needs to be kept null if null, so possible values here are: true|false|null
    converted_data["isAnonymous"] = usage_metric.isanonymous
    # lastlogin needs to be kept null if null, so possible values here are: <epoch_time>|null
    converted_data["lastlogin"] = convert_datetime_to_epoch(usage_metric.lastlogin) if usage_metric.lastlogin else None
    converted_data["name"] = hash_data(usage_metric.name)
    converted_data["usertype"] = usage_metric.usertype
    converted_data["webserviceuser"] = usage_metric.webserviceuser

    if to_file:
        # fields that needs only in file
        converted_data["created_at"] = str(datetime.now())
        converted_data["schema_version"] = usage_metrics_schema_version
        converted_data["server_id"] = server_id

    return converted_data


def get_hashed_email_domain(name, email):
    # prefer email from the name field over the email field
    hashed_email_domain = extract_and_hash_domain_from_email(name)
    if not hashed_email_domain:
        hashed_email_domain = extract_and_hash_domain_from_email(email)
    return hashed_email_domain


def convert_datetime_to_epoch(lastlogin):
    # Convert datetime in epoch format
    lastlogin_string = str(lastlogin)
    parsed_datetime = datetime.strptime(lastlogin_string, "%Y-%m-%d %H:%M:%S.%f")
    parsed_datetime_tuple = parsed_datetime.timetuple()
    epoch = mktime(parsed_datetime_tuple)
    return int(epoch)


def get_email_columns(config, db_conn):
    # simple cursor
    with db_conn.cursor() as db_cursor: 
        email_table_and_columns = dict()
        user_specialization_tables = get_user_specialization_tables(db_cursor)

        # exit if no entities found
        if len(user_specialization_tables) == 0:
            return email_table_and_columns

        query = sql.SQL("""
            SELECT  table_name, column_name 
            FROM    information_schema.columns 
            WHERE   table_name IN ({table_names}) 
                    {columns_clause}
        """)

        columns_clause = sql.SQL("AND column_name LIKE '%%mail%%'")

        # getting user defined email columns if they are provided
        user_custom_email_columns = get_user_custom_email_columns(config, user_specialization_tables)

        #  changing query if user defined email columns provided and valid
        if user_custom_email_columns:
            columns_clause = sql.SQL(
                "AND (column_name LIKE '%%mail%%' OR column_name IN ({user_custom_columns}))"
            ).format(
                user_custom_columns = sql.SQL(', ').join(
                    sql.Literal(column) for column in user_custom_email_columns
                )
            )

        user_specialization_tables = sql.SQL(', ').join(
            sql.Literal(table) for table in user_specialization_tables
        )

        # combining final query
        query = query.format(
            table_names = user_specialization_tables,
            columns_clause = columns_clause
        )
        
        logger.debug("Executing query: %s", query.as_string(db_cursor))

        db_cursor.execute(query)
        result = db_cursor.fetchall()

        for row in result:
            table = row[0].strip().lower()
            column = row[1].strip().lower()
            if table and column:
                email_table_and_columns[table] = column
    
    logger.debug("Probable tables and columns that may have an email address are: %s", email_table_and_columns)

    return email_table_and_columns


def convert_found_user_attributes_to_query_additions(table_email_columns):
    # exit if there are no user email tables found
    if not table_email_columns:
        return sql.SQL(''), sql.SQL('')

    # making 'email attribute' and 'joins' part of the query
    projection = list()
    joins = list()
    
    # iterate over the table_email_columns to form the CONCAT and JOIN part of the query
    for i, (table, column) in enumerate(table_email_columns.items()):
        mailfield_prefix = "mailfield_" + str(i)

        projection.append(sql.SQL("{mailfield}.{column}").format(
            mailfield = sql.Identifier(mailfield_prefix),
            column = sql.Identifier(column)
        ))

        joins.append(sql.SQL("LEFT JOIN {table} {mailfield} on {mailfield}.id = u.id").format(
            table = sql.Identifier(table),
            mailfield = sql.Identifier(mailfield_prefix)
        ))
    
    email_part = sql.SQL(", CONCAT({}) as email").format(sql.SQL(', ').join(projection))
    joins_part = sql.SQL(' ').join(joins)

    return email_part, joins_part


def get_batch_db_cursor(config, conn):
    # separate db cursor to use batches
    batch_cur = conn.cursor(
        # Cursor name should be provided here to use server-side cursor and optimize memory usage: 
        # Psycopg2 will load all of the query into memory if the name isn’t specified for the cursor 
        # object even in case of fetchone() or fetchmany() and batch processing used. If name is 
        # specified then cursor will be created on the server side that allows to avoid additional 
        # memory usage.
        name = "m2ee_metering_cursor",
        cursor_factory = NamedTupleCursor
    )

    # setting batch size if specified, otherwise default batch=2000 will be used
    batch_size = config.get_usage_metrics_db_query_batch_size()

    if batch_size:
        batch_cur.itersize = batch_size

    return batch_cur


def get_user_specialization_tables(db_cursor):
    query = "SELECT DISTINCT submetaobjectname FROM system$user;"

    logger.debug("Executing query: %s", query)

    db_cursor.execute(query)
    result = [r[0] for r in db_cursor.fetchall()]

    logger.debug("User specialization tables are: <%s>", result)

    user_specialization_tables = []
    for submetaobject in result:
        # ignore the None values and System.User table
        if submetaobject is not None and submetaobject != "System.User":
            # cast user attribute names to Postgres syntax
            submetaobject = submetaobject.strip().lower().replace('.', '$')
            # ignore empty rows
            if submetaobject:
                # checking if user provided attribute is valid SQL literal and adding it to the result array
                user_specialization_tables.append(submetaobject)

    return user_specialization_tables
        

def get_user_custom_email_columns(config, user_specialization_tables):
    usage_metrics_email_fields = config.get_usage_metrics_email_fields()
    
    # exit if custom email fields are not provided
    if not usage_metrics_email_fields:
        return ""

    valid_attributes = preprocess_and_validate_attributes(usage_metrics_email_fields)
    
    if not valid_attributes:
        return ""

    user_defined_table_and_column_names = valid_attributes.split(',')
    
    # extract column names
    columnNames = list()
    for attribute in user_defined_table_and_column_names:
        separatorPosition = attribute.rindex(".")
        
        table = attribute[:separatorPosition]
        table = table.replace(".", "$")
        
        column = attribute[separatorPosition+1:]
        
        # user custom attributes must be specializations of 'System.User'
        if table in user_specialization_tables:
            columnNames.append(column)
        else:
            logger.warn(
                "Table '%s' specified in USAGE_METRICS_EMAIL_FIELDS either doesn't " +
                "exist in the database or is not a 'System.User' specialization",
                table,
            )

    return columnNames


def preprocess_and_validate_attributes(usage_metrics_email_fields):
    # cleaning value from spaces
    # using for loop to keep it works with python 2 and 3 versions
    usage_metrics_email_fields = "".join(c for c in usage_metrics_email_fields if c not in string.whitespace)
    usage_metrics_email_fields = usage_metrics_email_fields.lower()

    # validating usage_metrics_email_fields value
	# valid form is "Module1.Entity1.Attribute1,Module2.Entity2.Attribute2,..."
	# PostgreSQL rules also considered: https://www.postgresql.org/docs/current/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS
    validationPattern = "^([a-zA-Z_]\w+\.[a-zA-Z_]\w+\.[a-zA-Z_]\w+,?)+$"
    isValid = re.match(validationPattern, usage_metrics_email_fields)
    
    # exit if usage_metrics_email_fields has invalid value
    if not isValid:
        logger.error(
            "usage_metrics_email_fields property value is invalid or contains unsupported characters: %s", 
            usage_metrics_email_fields
        )
        return ""

    return usage_metrics_email_fields
    

def extract_and_hash_domain_from_email(email):
    if not isinstance(email, str):
        return ""

    if not email:
        return ""

    domain = ""
    if email.find("@") != -1:
        domain = str(email).split("@")[1]

    if len(domain) >= 2:
        return hash_data(domain)
    else:
        return ""


def hash_data(name):
    salt = [53, 14, 215, 17, 147, 90, 22, 81, 48, 249, 140, 146, 201, 247, 182, 18, 218, 242, 114, 5, 255, 202, 227,
            242, 126, 235, 162, 38, 52, 150, 95, 193]
    salt_byte_array = bytes(salt)

    encoded_name = name.encode()
    byte_array = bytearray(encoded_name)
    
    h = hashlib.sha256()
    h.update(salt_byte_array)
    h.update(byte_array)
    
    return h.hexdigest()


def create_output_dir(config, file_suffix):
    output_path = config.get_usage_metrics_output_file_path() + "/usage_metrics_output_" + file_suffix
    # if not path.exists(output_path):
    makedirs(output_path)
    return output_path


def generate_md5(file):
    generated_md5 = hashlib.md5(open(file, "rb").read()).hexdigest()
    with open(file + '.md5', 'w') as md5_file:
        md5_file.write(generated_md5)


def zip_files(directory, file_suffix):
    shutil.make_archive('mendix_usage_metrics_' + file_suffix, 'zip', directory)

