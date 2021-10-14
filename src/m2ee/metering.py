#
# Copyright (C) 2021 Mendix. All rights reserved.
#
import string
import datetime
import hashlib
import json
import logging
import re

import psycopg2
import socket

from m2ee.client import M2EEAdminNotAvailable
from psycopg2 import sql
from psycopg2.extras import NamedTupleCursor
from time import mktime
from time import time
from datetime import datetime

logger = logging.getLogger(__name__)
usage_metrics_schema_version = "1.2"

try:
    import httplib2
except ImportError:
    logger.critical(
        "Failed to import httplib2. This module is needed by "
        "m2ee. Please povide it on the python library path"
    )
    raise

def metering_export_usage_metrics(m2ee):
    try:
        logger.info("Begin exporting usage metrics %s", datetime.now())

        # trying to get server_id at the very beginning to 
        # drop futher execution if Mendix app is not running
        # and server_id is unavailable
        server_id = metering_get_server_id(m2ee.client)

        config = m2ee.config

        db_cursor, db_conn = open_db_connection(config)
        
        # get the number of users
        user_count = get_users_count(db_cursor)

        # get page size from config
        page_size = config.get_usage_metrics_page_size()
        if page_size == 0:
            # user does not want pagination
            page_size = user_count

        # starting export
        is_subscription_service_available = metering_check_subscription_service_availability(config)
        if is_subscription_service_available:
            # export to Subscription Service directly if it is available (for connected environments)
            logger.info("Exporting to the Subscription Service")
            metering_export_to_subscription_service(config, user_count, db_cursor, page_size, server_id)
        else:
            logger.info("Exporting to file")
            # export to file (for disconnected environments)
            metering_export_to_file(config, user_count, db_cursor, page_size, server_id)

    except Exception as e:
        logger.error(e)
    finally:
        close_db_connection(db_cursor, db_conn)


def open_db_connection(config):
    try:
        pg_config = config.get_pg_environment() 
        db_name = pg_config['PGDATABASE']
        db_username = pg_config['PGUSER']
        db_password = pg_config['PGPASSWORD']

        conn = psycopg2.connect(
            database = db_name, 
            user = db_username, 
            password = db_password
        )

        cur = conn.cursor(cursor_factory = NamedTupleCursor)

        logger.debug("Usage metering: connected to database")

        return cur, conn
    except Exception as e:
        logger.error(e)


def close_db_connection(cur, conn):
    try:
        cur.close()
        conn.close()

        logger.debug("Usage metering: database is disconnected")
    except Exception as e:
        logger.error(e)


def get_users_count(cur):
    query = "SELECT count(id) FROM system$user;"
    
    logger.debug("Executing query: %s", query)

    cur.execute(query)
    result = cur.fetchone()[0]

    logger.debug("Users count: %s", result)

    return result


def metering_usage_query(config, db_cursor, user_count, page_size=0, offset=0):
    if page_size < user_count:
        logger.info(
            "Processing %d to %d of %d records", 
            offset + 1, 
            offset + page_size, 
            user_count
        )
    
    try:
        logger.debug("Begin usage metering query %s", datetime.now())
        
        # the base query
        query = sql.SQL("""
            SELECT 
                u.name, 
                u.lastlogin, 
                u.webserviceuser, 
                u.blocked, 
                u.active, 
                u.isanonymous as is_anonymous,
                ur.usertype
                {email} 
            FROM system$user u 
            LEFT JOIN system$userreportinfo_user ur_u ON u.id = ur_u.system$userid 
            LEFT JOIN system$userreportinfo ur ON ur.id = ur_u.system$userreportinfoid
            {extra_joins}
            WHERE u.name IS NOT NULL 
            ORDER BY u.id
            {limit}
        """)

        # looking for email entities
        email_table_and_columns = metering_get_email_columns(config, db_cursor)

        # preparing found email tables and columns to join the query as {email} and {extra_joins}
        email_part, joins_part = metering_convert_found_user_attributes_to_query_additions(email_table_and_columns)

        # preparing LIMIT part of the query
        if page_size > 0:
            limit_literal = sql.SQL("LIMIT {page_size} OFFSET {offset}").format(
                page_size = sql.Literal(page_size),
                offset = sql.Literal(offset)
            )
        
        # combining final query
        query = query.format(
            email = email_part,
            extra_joins = joins_part,
            limit = limit_literal
        )

        logger.debug("Constructed query: %s", query.as_string(db_cursor))
        
        # executing query
        db_cursor.execute(query)
        result = db_cursor.fetchall()

        logger.debug("Users found: %s", len(result))
        logger.debug("End usage metering query %s", datetime.now())

        return result
    except Exception as e:
        logger.error(e)


def metering_check_subscription_service_availability(config):
    url = config.get_usage_metrics_subscription_service_uri()

    try:
        http = httplib2.Http()
        response = http.request(url, 'POST')

        if int(response[0]['status']) == 200:
            return True
    except:
        pass
    
    return False


def metering_export_to_subscription_service(config, user_count, db_cursor, page_size, server_id):

    for offset in range(0, user_count, page_size):
        usage_metering_result = metering_usage_query(config, db_cursor, user_count, page_size, offset)

        usage_metrics = []

        for usage_metric in usage_metering_result:
            metric_to_export = metering_convert_data_for_export(usage_metric, server_id)
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

    try:
        h = httplib2.Http(
            timeout = subscription_service_timeout, 
            proxy_info = None   # httplib does not like os.fork
        )

        logger.trace("Metering request headers: %s" % headers)
        logger.trace("Metering request body: %s" % body)

        response_headers, response_bytes = h.request(url, "POST", body, headers)
        response_body = response_bytes.decode('utf-8')

        logger.trace("Subscription Service response: %s" % response_body)
        
        if (response_headers['status'] != "200"):
            logger.error(
                "Non OK http status code: %s %s" %
                (response_headers, response_body)
            )
            return
        
        response = json.loads(response_body)
        result = response['licensekey'] 
        
        if not result:
            error_msg = response['logmessages'] if response['logmessages'] else ""
            logger.error("Subscription Service error %s" % error_msg)
        
        logger.info("Usage metrics exported %s to Subscription Service", datetime.now())
        
    except AttributeError as e:
        # httplib 0.6 throws this in case of a connection refused :-|
        if str(e) == "'NoneType' object has no attribute 'makefile'":
            message = "Subscription Service API not available for requests."
            logger.trace("%s (%s: %s)" % (message, type(e), e))
    except socket.timeout as e:
        message = "Subscription Service API does not respond. "
        "Timeout reached after %s seconds." % subscription_service_timeout
        logger.trace(message)
    except socket.error as e:
        message = "Subscription Service API not available for requests: (%s: %s)" % (type(e), e)
        logger.trace(message)


def metering_export_to_file(config, user_count, db_cursor, page_size, server_id):
    try:
        # create export file
        output_file_name = config.get_usage_metrics_output_file_name() + "_" + str(int(time())) + ".json"

        out_file = open(output_file_name, "w")

        # dump usage metering data to file
        out_file.write("[\n")
        for offset in range(0, user_count, page_size):
            usage_metering_result = metering_usage_query(config, db_cursor, user_count, page_size, offset)
            metering_convert_and_export_to_file(usage_metering_result, server_id, out_file)
        out_file.write("\n]")

        logger.info("Usage metrics exported %s to %s", datetime.now(), output_file_name)
    except Exception as e:
        logger.error(e)
    finally:
        out_file.close()


def metering_convert_and_export_to_file(user_usage_metric_data, server_id, out_file):
    last_element_index = len(user_usage_metric_data) - 1

    for i, usage_metric in enumerate(user_usage_metric_data):
        export_data = metering_convert_data_for_export(usage_metric, server_id, True)
        
        # using json.dump() instead of json.dumps() and dump each row separately here 
        # to reduce memory usage since json.dumps() could consume a lot of memory for a 
        # large number of users (e.g. it takes ~3Gb for 1 million users) 
        json.dump(export_data, out_file, indent = 4, sort_keys = True)

        if i != last_element_index:
            out_file.write(",\n")


def metering_convert_data_for_export(usage_metric, server_id, to_file = False):
    converted_data = {}

    converted_data["active"] = usage_metric.active
    converted_data["blocked"] = usage_metric.blocked
    # prefer email from the name field over the email field
    converted_data["emailDomain"] = metering_get_hashed_email_domain(usage_metric.name, usage_metric.email)
    # isAnonymous needs to be kept null if null, so possible values here are: true|false|null
    converted_data["isAnonymous"] = usage_metric.is_anonymous
    # lastlogin needs to be kept null if null, so possible values here are: <epoch_time>|null
    converted_data["lastlogin"] = metering_convert_datetime_to_epoch(usage_metric.lastlogin) if usage_metric.lastlogin else None
    converted_data["name"] = metering_encrypt(usage_metric.name)
    converted_data["webserviceuser"] = usage_metric.webserviceuser

    if to_file:
        # fields that needs only in file
        converted_data["created_at"] = str(datetime.now())
        converted_data["schema_version"] = usage_metrics_schema_version
        converted_data["server_id"] = server_id

    return converted_data


def metering_get_hashed_email_domain(name, email):
    # prefer email from the name field over the email field
    hashed_email_domain = metering_extract_and_hash_domain_from_email(name)
    if not hashed_email_domain:
        hashed_email_domain = metering_extract_and_hash_domain_from_email(email)
    return hashed_email_domain


def metering_convert_datetime_to_epoch(lastlogin):
    # Convert datetime in epoch format
    lastlogin_string = str(lastlogin)
    parsed_datetime = datetime.strptime(lastlogin_string, "%Y-%m-%d %H:%M:%S.%f")
    parsed_datetime_tuple = parsed_datetime.timetuple()
    epoch = mktime(parsed_datetime_tuple)
    return int(epoch)


def metering_get_email_columns(config, db_cursor):
    try:
        email_table_and_columns = dict()

        user_specialization_tables = metering_get_user_specialization_tables(db_cursor)

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
        user_custom_email_columns = metering_get_user_custom_email_columns(config, user_specialization_tables)

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
    except Exception as e:
        logger.error(e)


def metering_convert_found_user_attributes_to_query_additions(table_email_columns):
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


def metering_get_user_specialization_tables(db_cursor):
    try:
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
    except Exception as e:
        logger.error(e)
        

def metering_get_user_custom_email_columns(config, user_specialization_tables):
    usage_metrics_email_fields = config.get_usage_metrics_email_fields()
    
    # exit if custom email fields are not provided
    if not usage_metrics_email_fields:
        return ""

    valid_attributes = metering_preprocess_and_validate_attributes(usage_metrics_email_fields)
    
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
            logger.error(
                "Table '%s' specified in USAGE_METRICS_EMAIL_FIELDS either doesn't " +
                "exist in the database or is not a 'System.User' specialization",
                table,
            )

    return columnNames


def metering_preprocess_and_validate_attributes(usage_metrics_email_fields):
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
    

def metering_extract_and_hash_domain_from_email(email):
    if not isinstance(email, str):
        return ""

    if not email:
        return ""

    domain = ""
    if email.find("@") != -1:
        domain = str(email).split("@")[1]

    if len(domain) >= 2:
        return metering_encrypt(domain)
    else:
        return ""


def metering_encrypt(name):
    salt = [53, 14, 215, 17, 147, 90, 22, 81, 48, 249, 140, 146, 201, 247, 182, 18, 218, 242, 114, 5, 255, 202, 227,
            242, 126, 235, 162, 38, 52, 150, 95, 193]
    salt_byte_array = bytes(salt)

    encoded_name = name.encode()
    byte_array = bytearray(encoded_name)
    
    h = hashlib.sha256()
    h.update(salt_byte_array)
    h.update(byte_array)
    
    return h.hexdigest()


def metering_get_server_id(client):
    try:
        return client.get_license_information()["license_id"]
    except M2EEAdminNotAvailable as e:
        raise Exception("The application process is not running.")
