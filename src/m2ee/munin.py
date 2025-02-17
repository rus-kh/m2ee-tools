#
# Copyright (C) 2009 Mendix. All rights reserved.
#

from __future__ import print_function
import json
import logging
import os
from m2ee.client import M2EEAdminException, M2EEAdminNotAvailable, \
    M2EEAdminHTTPException, M2EEAdminTimeout
import m2ee.smaps as smaps
import m2ee.pgutil

logger = logging.getLogger(__name__)

default_stats = {
    "languages": ["en_US"],
    "entities": 0,
    "threadpool": {
        "threads_priority": 0,
        "max_threads": 0,
        "min_threads": 0,
        "max_idle_time_s": 0,
        "max_queued": -0,
        "threads": 0,
        "idle_threads": 0,
        "max_stop_time_s": 0
    },
    "memory": {
        "init_heap": 0,
        "code": 0,
        "used_heap": 0,
        "survivor": 0,
        "max_nonheap": 0,
        "committed_heap": 0,
        "tenured": 0,
        "permanent": 0,
        "used_nonheap": 0,
        "eden": 0,
        "init_nonheap": 0,
        "committed_nonheap": 0,
        "max_heap": 0
    },
    "sessions": {
        "named_users": 0,
        "anonymous_sessions": 0,
        "named_user_sessions": 0,
        "user_sessions": {}
    },
    "requests": {
        "": 0,
        "debugger/": 0,
        "ws/": 0,
        "xas/": 0,
        "ws-doc/": 0,
        "file": 0
    },
    "cache": {
        "total_count": 0,
        "disk_count": 0,
        "memory_count": 0
    },
    "jetty": {
        "max_idle_time_s": 0,
        "current_connections": 0,
        "max_connections": 0,
        "max_idle_time_s_low_resources": 0
    },
    "connectionbus": {
        "insert": 0,
        "transaction": 0,
        "update": 0,
        "select": 0,
        "delete": 0
    }
}


def print_config(m2, name):
    stats, java_version = get_stats('config', m2)
    if stats is not None:
        options = m2.config.get_munin_options()
        print_requests_config(name, stats)
        print_connectionbus_config(name, m2, stats)
        print_sessions_config(name, stats, options.get('graph_total_named_users', True))
        print_jvmheap_config(name, stats)
        print_threadpool_config(name, stats)
        print_cache_config(name, stats)
        print_jvm_threads_config(name, stats)
        print_jvm_process_memory_config(name)
    if m2.config.is_using_postgresql():
        print_pg_stat_database_config(name)
        print_pg_stat_activity_config(name)
        print_pg_table_index_size_config(name)


def print_values(m2, name):
    stats, java_version = get_stats('values', m2)
    if stats is not None:
        options = m2.config.get_munin_options()
        print_requests_values(name, stats)
        print_connectionbus_values(name, stats)
        print_sessions_values(name, stats, options.get('graph_total_named_users', True))
        print_jvmheap_values(name, stats)
        print_threadpool_values(name, stats)
        print_cache_values(name, stats)
        print_jvm_threads_values(name, stats)
        print_jvm_process_memory_values(name, stats, m2.runner.get_pid(), java_version)
    if m2.config.is_using_postgresql():
        print_pg_stat_database_values(name, m2)
        print_pg_stat_activity_values(name, m2)
        print_pg_table_index_size_values(name, m2)


def guess_java_version(m2, runtime_version, stats):
    about = m2.client.about(timeout=5)
    if 'java_version' in about:
        java_version = about['java_version']
        java_major, java_minor, _ = java_version.split('.')
        return int(java_minor)
    if runtime_version // 6:
        return 8
    if runtime_version // 5:
        m = stats['memory']
        if m['used_nonheap'] - m['code'] - m['permanent'] == 0:
            return 7
        return 8
    return None


def get_stats(action, m2):
    # place to store last known good statistics result to be used for munin
    # config when the app is down or b0rked
    options = m2.config.get_munin_options()
    config_cache = options.get('config_cache',
                               os.path.join(m2.config.get_default_dotm2ee_directory(),
                                            'munin-cache.json'))
    stats = None
    java_version = None
    try:
        stats, java_version = get_stats_from_runtime(m2)
        write_last_known_good_stats_cache(stats, config_cache)
    except (M2EEAdminException, M2EEAdminNotAvailable,
            M2EEAdminHTTPException, M2EEAdminTimeout) as e:
        if not isinstance(e, M2EEAdminNotAvailable) or m2.runner.check_pid():
            logger.error(e)
        if action == 'config':
            return get_last_known_good_or_fake_stats(config_cache), java_version
    return stats, java_version


def get_last_known_good_or_fake_stats(config_cache):
    stats = read_stats_from_last_known_good_stats_cache(config_cache)
    if stats is not None:
        logger.debug("Reusing last known statistics.")
    else:
        logger.debug("No last known good statistics found, using fake statistics.")
        stats = default_stats
    return stats


def get_stats_from_runtime(m2):
    stats = {}
    logger.debug("trying to fetch runtime/server statistics")
    stats.update(m2.client.runtime_statistics(timeout=5))
    stats.update(m2.client.server_statistics(timeout=5))
    if type(stats['requests']) == list:
        # convert back to normal, whraagh
        bork = {}
        for x in stats['requests']:
            bork[x['name']] = x['value']
        stats['requests'] = bork

    runtime_version = m2.config.get_runtime_version()
    if runtime_version is not None and runtime_version >= 3.2:
        stats['threads'] = len(m2.client.get_all_thread_stack_traces(timeout=5))

    java_version = guess_java_version(m2, runtime_version, stats)
    if 'memorypools' in stats['memory']:
        memorypools = stats['memory']['memorypools']
        if java_version == 7:
            stats['memory']['code'] = memorypools[0]['usage']
            stats['memory']['permanent'] = memorypools[4]['usage']
            stats['memory']['eden'] = memorypools[1]['usage']
            stats['memory']['survivor'] = memorypools[2]['usage']
            stats['memory']['tenured'] = memorypools[3]['usage']
        else:
            stats['memory']['code'] = memorypools[0]['usage']
            stats['memory']['permanent'] = memorypools[2]['usage']
            stats['memory']['eden'] = memorypools[3]['usage']
            stats['memory']['survivor'] = memorypools[4]['usage']
            stats['memory']['tenured'] = memorypools[5]['usage']
    elif java_version >= 8:
        memory = stats['memory']
        metaspace = memory['eden']
        eden = memory['tenured']
        survivor = memory['permanent']
        old = memory['used_heap'] - eden - survivor
        memory['permanent'] = metaspace
        memory['eden'] = eden
        memory['survivor'] = survivor
        memory['tenured'] = old
    return stats, java_version


def write_last_known_good_stats_cache(stats, config_cache):
    logger.debug("Writing munin cache to %s" % config_cache)
    try:
        with open(config_cache, 'w+') as f:
            f.write(json.dumps(stats))
    except Exception as e:
        logger.error("Error writing munin config cache to %s: %s" %
                     (config_cache, e))


def read_stats_from_last_known_good_stats_cache(config_cache):
    stats = None
    logger.debug("Loading munin cache from %s" % config_cache)
    try:
        with open(config_cache) as f:
            stats = json.load(f)
    except IOError as e:
        logger.error("Error reading munin cache file %s: %s" %
                     (config_cache, e))
    except ValueError as e:
        logger.error("Error parsing munin cache file %s: %s" %
                     (config_cache, e))
    return stats


def print_requests_config(name, stats):
    print("multigraph mxruntime_requests_%s" % name)
    print("graph_args --base 1000 -l 0")
    print("graph_vlabel Requests per second")
    print("graph_title %s - MxRuntime Requests" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the amount of requests this MxRuntime handles")
    for sub in stats['requests'].keys():
        substrip = '_' + sub.strip('/').replace('-', '_')
        if sub != '':
            subname = sub
        else:
            subname = '/'
        print("%s.label %s" % (substrip, subname))
        print("%s.draw LINE1" % substrip)
        print("%s.info amount of requests this MxRuntime handles on %s" % (substrip, subname))
        print("%s.type DERIVE" % substrip)
        print("%s.min 0" % substrip)
    print("")


def print_requests_values(name, stats):
    print("multigraph mxruntime_requests_%s" % name)
    for sub, count in stats['requests'].items():
        substrip = '_' + sub.strip('/').replace('-', '_')
        print("%s.value %s" % (substrip, count))
    print("")


def print_connectionbus_config(name, m2, stats):
    if 'connectionbus' not in stats:
        return
    print("multigraph mxruntime_connectionbus_%s" % name)
    print("graph_args --base 1000 -l 0")
    print("graph_vlabel Statements per second")
    if m2.config.is_using_postgresql():
        print("graph_title %s - PostgreSQL queries" % name)
    else:
        print("graph_title %s - Database queries" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the amount of executed queries by type")
    for s in ('select', 'insert', 'update', 'delete'):
        print("%s.label %ss" % (s, s))
        print("%s.draw LINE1" % s)
        print("%s.info amount of %ss" % (s, s))
        print("%s.type DERIVE" % s)
        print("%s.min 0" % s)
    print("")


def print_connectionbus_values(name, stats):
    if 'connectionbus' not in stats:
        return
    print("multigraph mxruntime_connectionbus_%s" % name)
    for s in ('select', 'insert', 'update', 'delete'):
        print("%s.value %s" % (s, stats['connectionbus'][s]))
    print("")


def print_sessions_config(name, stats, graph_total_named_users):
    print("multigraph mxruntime_sessions_%s" % name)
    print("graph_args --base 1000 -l 0")
    print("graph_vlabel Concurrent user sessions")
    print("graph_title %s - MxRuntime Users" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the amount of user accounts and sessions")
    if graph_total_named_users:
        print("named_users.label named users")
        print("named_users.draw LINE1")
        print("named_users.info total amount of named users in the application")
    print("named_user_sessions.label concurrent named user sessions")
    print("named_user_sessions.draw LINE1")
    print("named_user_sessions.info amount of concurrent named user sessions")
    print("anonymous_sessions.label concurrent anonymous user sessions")
    print("anonymous_sessions.draw LINE1")
    print("anonymous_sessions.info amount of concurrent anonymous user sessions")
    print("")


def print_sessions_values(name, stats, graph_total_named_users):
    print("multigraph mxruntime_sessions_%s" % name)
    if graph_total_named_users:
        print("named_users.value %s" % stats['sessions']['named_users'])
    print("named_user_sessions.value %s" %
          stats['sessions']['named_user_sessions'])
    print("anonymous_sessions.value %s" %
          stats['sessions']['anonymous_sessions'])
    print("")


def print_jvmheap_config(name, stats):
    print("multigraph mxruntime_jvmheap_%s" % name)
    print("graph_args --base 1024 -l 0")
    print("graph_vlabel Bytes")
    print("graph_title %s - JVM Heap Memory Usage" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows memory pool information on the Java JVM")
    print("tenured.label tenured generation")
    print("tenured.draw AREA")
    print("tenured.info Old generation of the heap that holds long living objects")
    print("tenured.colour COLOUR2")
    print("survivor.label survivor space")
    print("survivor.draw STACK")
    print("survivor.info Survivor Space of the Young Generation")
    print("survivor.colour COLOUR3")
    print("eden.label eden space")
    print("eden.draw STACK")
    print("eden.info Objects are created in Eden")
    print("eden.colour COLOUR4")
    print("free.label unused")
    print("free.draw STACK")
    print("free.info Unused memory reserved for use by the JVM heap")
    print("free.colour COLOUR5")
    print("limit.label heap size limit")
    print("limit.draw LINE1")
    print("limit.info Java Heap memory usage limit")
    print("limit.colour COLOUR6")
    print("")


def print_jvmheap_values(name, stats):
    print("multigraph mxruntime_jvmheap_%s" % name)
    memory = stats['memory']
    for k in ['tenured', 'survivor', 'eden']:
        print('%s.value %s' % (k, memory[k]))
    free = (memory['max_heap'] - memory['used_heap'])
    print("free.value %s" % free)
    print("limit.value %s" % memory['max_heap'])
    print("")


def print_threadpool_config(name, stats):
    if "threadpool" not in stats:
        return
    print("multigraph m2eeserver_threadpool_%s" % name)
    print("graph_args --base 1000 -l 0")
    print("graph_vlabel Jetty Threadpool")
    print("graph_title %s - Jetty Threadpool" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows threadpool usage information on Jetty")
    print("min_threads.label min threads")
    print("min_threads.draw LINE1")
    print("min_threads.info Minimum number of threads")
    print("max_threads.label max threads")
    print("max_threads.draw LINE1")
    print("max_threads.info Maximum number of threads")
    print("active_threads.label active threads")
    print("active_threads.draw LINE1")
    print("active_threads.info Active thread count")
    print("threadpool_size.label threadpool size")
    print("threadpool_size.draw LINE1")
    print("threadpool_size.info Current threadpool size")
    print("")


def print_threadpool_values(name, stats):
    if "threadpool" not in stats:
        return

    min_threads = stats['threadpool']['min_threads']
    max_threads = stats['threadpool']['max_threads']
    threadpool_size = stats['threadpool']['threads']
    idle_threads = stats['threadpool']['idle_threads']
    active_threads = threadpool_size - idle_threads

    print("multigraph m2eeserver_threadpool_%s" % name)
    print("min_threads.value %s" % min_threads)
    print("max_threads.value %s" % max_threads)
    print("active_threads.value %s" % active_threads)
    print("threadpool_size.value %s" % threadpool_size)
    print("")


def print_cache_config(name, stats):
    if "cache" not in stats:
        return
    print("multigraph mxruntime_cache_%s" % name)
    print("graph_args --base 1000 -l 0")
    print("graph_vlabel objects")
    print("graph_title %s - Object Cache" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the total amount of objects in the runtime object cache")
    print("total.label Objects in cache")
    print("total.draw LINE1")
    print("total.info Total amount of objects")
    print("")


def print_cache_values(name, stats):
    if "cache" not in stats:
        return
    print("multigraph mxruntime_cache_%s" % name)
    print("total.value %s" % stats['cache']['total_count'])
    print("")


def print_jvm_threads_config(name, stats):
    if "threads" not in stats:
        return
    print("multigraph mxruntime_threads_%s" % name)
    print("graph_args --base 1000 -l 0")
    print("graph_vlabel threads")
    print("graph_title %s - JVM Threads" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the total amount of threads in the JVM process")
    print("total.label threads")
    print("total.draw LINE1")
    print("total.info Total amount of threads in the JVM process")
    print("")


def print_jvm_threads_values(name, stats):
    if "threads" not in stats:
        return
    print("multigraph mxruntime_threads_%s" % name)
    print("total.value %s" % stats['threads'])
    print("")


def print_jvm_process_memory_config(name):
    if not smaps.has_smaps('self'):
        return
    print("multigraph mxruntime_jvm_process_memory_%s" % name)
    print("graph_args --base 1024 -l 0")
    print("graph_vlabel Bytes")
    print("graph_title %s - JVM Process Memory Usage" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the total memory usage of the Java JVM process")
    print("nativecode.label native code")
    print("nativecode.draw AREA")
    print("nativecode.info Native program code, e.g. the java binary itself")
    print("jar.label jar files")
    print("jar.draw STACK")
    print("jar.info JAR file contents loaded into memory")
    print("tenured.label tenured generation")
    print("tenured.draw STACK")
    print("tenured.info Old generation of the Java Heap that holds long living objects")
    print("survivor.label survivor space")
    print("survivor.draw STACK")
    print("survivor.info Survivor Space of the Young Generation, Java Heap")
    print("eden.label eden space")
    print("eden.draw STACK")
    print("eden.info Objects are created in Eden, Java Heap")
    print("javaheap.label unused java heap")
    print("javaheap.draw STACK")
    print("javaheap.info Unused Java Heap")
    print("permanent.label permanent generation")
    print("permanent.draw STACK")
    print("permanent.info Non-heap memory used to store bytecode versions of classes")
    print("codecache.label code cache")
    print("codecache.draw STACK")
    print("codecache.info Non-heap memory used for compilation and storage of native code")
    print("nativemem.label native memory")
    print("nativemem.draw STACK")
    print("nativemem.info Native heap and memory arenas")
    print("stacks.label thread stacks")
    print("stacks.draw STACK")
    print("stacks.info Thread stacks")
    print("other.label other")
    print("other.draw STACK")
    print("other.info Other, unknown, undetermined memory usage")
    print("total.label total")
    print("total.draw LINE1")
    print("total.info Total memory usage")
    print("")


def print_jvm_process_memory_values(name, stats, pid, java_version):
    if pid is None:
        return
    totals = smaps.get_smaps_rss_by_category(pid)
    if totals is None:
        return
    memory = stats['memory']
    print("multigraph mxruntime_jvm_process_memory_%s" % name)
    print("nativecode.value %s" % (totals[smaps.CATEGORY_CODE] * 1024))
    print("jar.value %s" % (totals[smaps.CATEGORY_JAR] * 1024))

    javaheap = totals[smaps.CATEGORY_JVM_HEAP] * 1024
    for k in ['tenured', 'survivor', 'eden']:
        print('%s.value %s' % (k, memory[k]))
    if java_version is not None and java_version >= 8:
        print("javaheap.value %s" % (javaheap - memory['used_heap'] - memory['code']))
    else:
        print("javaheap.value %s" %
              (javaheap - memory['used_heap'] - memory['code'] - memory['permanent']))

    nativemem = totals[smaps.CATEGORY_NATIVE_HEAP_ARENA] * 1024
    othermem = totals[smaps.CATEGORY_OTHER] * 1024
    print("permanent.value %s" % memory['permanent'])
    print("codecache.value %s" % memory['code'])
    if java_version is not None and java_version >= 8:
        print("nativemem.value %s" % (nativemem + othermem - memory['permanent']))
        print("other.value 0")
    else:
        print("nativemem.value %s" % nativemem)
        print("other.value %s" % othermem)

    print("stacks.value %s" % (totals[smaps.CATEGORY_THREAD_STACK] * 1024))
    print("total.value %s" % (sum(totals.values()) * 1024))
    print("")


def print_pg_stat_database_config(name):
    print("multigraph mxruntime_pg_stat_tuples_%s" % name)
    print("graph_args -l 0")
    print("graph_vlabel tuple mutations per second")
    print("graph_title %s - PostgreSQL tuple mutations" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows amount of tuple mutations")
    print("tup_inserted.label tuples inserted")
    print("tup_inserted.draw LINE1")
    print("tup_inserted.min 0")
    print("tup_inserted.type DERIVE")
    print("tup_inserted.info Number of inserts")
    print("tup_updated.label tuples updated")
    print("tup_updated.draw LINE1")
    print("tup_updated.min 0")
    print("tup_updated.type DERIVE")
    print("tup_updated.info Number of updates")
    print("tup_deleted.label tuples deleted")
    print("tup_deleted.draw LINE1")
    print("tup_deleted.min 0")
    print("tup_deleted.type DERIVE")
    print("tup_deleted.info Number of deletes")
    print("")


def print_pg_stat_database_values(name, m2):
    _, _, tup_inserted, tup_updated, tup_deleted = m2ee.pgutil.pg_stat_database(m2.config)
    print("multigraph mxruntime_pg_stat_tuples_%s" % name)
    print("tup_inserted.value %s" % tup_inserted)
    print("tup_updated.value %s" % tup_updated)
    print("tup_deleted.value %s" % tup_deleted)
    print("")


def print_pg_stat_activity_config(name):
    print("multigraph mxruntime_pg_stat_activity_%s" % name)
    print("graph_args -l 0")
    print("graph_vlabel connections")
    print("graph_title %s - PostgreSQL connections" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the amount of open database connections")
    print("active.label active")
    print("active.draw AREA")
    print("active.info Amount of connections that currently execute a query "
          "(including this monitoring plugin)")
    print("idle_in_transaction.label idle in transaction")
    print("idle_in_transaction.draw STACK")
    print("idle_in_transaction.info Amount of idle transactions (e.g. running Mendix microflow "
          "which is not executing a database operation right now.")
    print("idle_in_transaction_aborted.label idle in transaction (aborted)")
    print("idle_in_transaction_aborted.draw STACK")
    print("idle_in_transaction_aborted.info Amount of idle transactions with errors")
    print("idle.label idle")
    print("idle.draw STACK")
    print("idle.info Amount of idle (unused) but open connections")
    print("total.label total")
    print("total.draw LINE1")
    print("total.colour 000000")
    print("total.info Total amount of open connections as seen by PostgreSQL "
          "(e.g. also including this monitoring query and things like backup dump operations)")
    print("limit.label mendix runtime limit")
    print("limit.draw LINE1")
    print("limit.info Limit on amount of open connections for the "
          "Mendix Runtime connection pooling")
    print("")


def print_pg_stat_activity_values(name, m2):
    activity = m2ee.pgutil.pg_stat_activity(m2.config)
    total = sum(activity.values())
    limit = m2.config.get_max_active_db_connections()
    print("multigraph mxruntime_pg_stat_activity_%s" % name)
    print("active.value %s" % activity.get('active', 0))
    print("idle.value %s" % activity.get('idle', 0))
    print("idle_in_transaction.value %s" % activity.get('idle in transaction', 0))
    print("idle_in_transaction_aborted.value %s" %
          activity.get('idle in transaction (aborted)', 0))
    print("total.value %s" % total)
    print("limit.value %s" % limit)
    print("")


def print_pg_table_index_size_config(name):
    print("multigraph mxruntime_pg_table_index_size_%s" % name)
    print("graph_args --base 1024 --lower-limit 0")
    print("graph_vlabel bytes")
    print("graph_title %s - PostgreSQL database size" % name)
    print("graph_category Mendix")
    print("graph_info This graph shows the distribution of table and index size in a database")
    print("tables.label tables")
    print("tables.draw AREA")
    print("tables.info Total disk space occupied by tables")
    print("indexes.label indexes")
    print("indexes.draw STACK")
    print("indexes.info Total disk space occupied by indexes")
    print("total.label total size")
    print("total.draw LINE0")
    print("total.colour 000000")
    print("total.info Total database size")
    print("")


def print_pg_table_index_size_values(name, m2):
    tables, indexes = m2ee.pgutil.pg_table_index_size(m2.config)
    print("multigraph mxruntime_pg_table_index_size_%s" % name)
    print("tables.value %s" % tables)
    print("indexes.value %s" % indexes)
    print("total.value %s" % (tables + indexes))
    print("")
