
#!/usr/bin/python3
from confluent_kafka import Consumer, KafkaError
import json
import re
import pandas as pd
import time
import shutil
from fastparquet import write as parqwrite
from fastparquet import ParquetFile
import os
import sys

# Variables - Should be setable by arguments at some point

envvars = {}
# envars['var'] = ['default', 'True/False Required', 'str/int']
#Kafka
envvars['zookeepers'] = ['', False, 'str']
envvars['kafka_id'] = ['', False, 'str']
envvars['bootstrap_brokers'] = ['', False, 'str']
envvars['offset_reset'] = ['earliest', False, 'str']
envvars['group_id'] = ['', True, 'str']
envvars['topic'] = ['', True, 'str']
envvars['loop_timeout'] = ["5.0", False, 'flt']

#Loop Control
envvars['rowmax'] = [50, False, 'int']
envvars['timemax'] = [60, False, 'int']
envvars['sizemax'] = [256000, False, 'int']

# Parquet Options
envvars['parq_offsets'] = [50000000, False, 'int']
envvars['parq_compress'] = ['SNAPPY', False, 'str']
envvars['filemaxsize'] = [8000000, False, 'int']
envvars['has_nulls'] = [1, False, 'bool']
envvars['uniq_env'] = ['HOSTNAME', False, 'str']

# Data Management
envvars['partition_field'] = ['', True, 'str']
envvars['partmaxage'] = ['600', False, 'int']
envvars['merge_file'] = [0, False, 'int']
envvars['remove_fields_on_fail'] = [0, False, 'int'] # If Json fails to import, should we try to remove_fields based on 'REMOVE_FIELDS' 
envvars['remove_fields'] = ['', False, 'str'] # Comma Sep list of fields to try to remove if failure on JSON import

# Destination Options
envvars['table_base'] = ['', True, 'str']
envvars['tmp_part_dir'] = ['.tmp', False, 'str']
envvars['write_live'] = [0, False, 'int']

# Debug
envvars['debug'] = [0, False, 'int']
#envvars['drop_req_body_on_error'] = [1, False, 'int']


loadedenv = {}


def main():

    global loadedenv
    loadedenv = loadenv(envvars)
    loadedenv['tmp_part'] = loadedenv['table_base'] + "/" + loadedenv['tmp_part_dir']
    loadedenv['uniq_val'] = os.environ[loadedenv['uniq_env']]
    if loadedenv['debug'] == 1:
        print(json.dumps(loadedenv, sort_keys=True, indent=4, separators=(',', ': ')))

    if not os.path.isdir(loadedenv['tmp_part']):
        os.makedirs(loadedenv['tmp_part'])

    # Get the Bootstrap brokers if it doesn't exist
    if loadedenv['bootstrap_brokers'] == "":
        if loadedenv['zookeepers'] == "":
            print("Must specify either Bootstrap servers via BOOTSTRAP_BROKERS or Zookeepers via ZOOKEEPERS")
            sys.exit(1)

        mybs = bootstrap_from_zk(loadedenv['zookeepers'], loadedenv['kafka_id'])
    if loadedenv['debug'] >= 1:
        print (mybs)

    # Create Consumer group to listen on the topic specified


    c = Consumer({'bootstrap.servers': mybs, 'group.id': loadedenv['group_id'], 'default.topic.config': {'auto.offset.reset': loadedenv['offset_reset']}})
    c.subscribe([loadedenv['topic']])

    # Initialize counters
    rowcnt = 0
    sizecnt = 0
    lastwrite = int(time.time()) - 1
    parqar = []
    part_ledger = {}
    curfile = loadedenv['uniq_val'] + "_curfile.parq"

    # Listen for messages
    running = True
    while running:
        curtime = int(time.time())
        timedelta = curtime - lastwrite
        try:
            message = c.poll(timeout=loadedenv['loop_timeout'])
        except KeyboardInterrupt:
            print("\n\nExiting per User Request")
            c.close()
            sys.exit(0)
        if message == None:
            # No message was found but we still want to check our stuff
            pass
        elif not message.error():
            rowcnt += 1
            # This is a message let's add it to our queue
            try:
                # This may not be the best way to approach this.
                val = message.value().decode('ascii', errors='replace')
            except:
                print(message.value())
                val = ""
            # Only write if we have a message
            if val != "":
                #Keep  Rough size count
                sizecnt += len(val)
                failedjson = 0
                try:
                    parqar.append(json.loads(val))
                except:
                    failedjson = 1
                    if loadedenv['remove_fields_on_fail'] == 1:
                        print("JSON Error likely due to binary in request - per config remove_field_on_fail - we are removing the the following fields and trying again")
                        while failedjson == 1:
                            repval = message.value()
                            for f in loadedenv['remove_fields'].split(","):
                                print("Trying to remove: %s" % f)
                                repval = re.sub(b'"' + f.encode() + b'":".+?","', b'"' + f.encode() + b'":"","', repval)
                                try:
                                    parqar.append(json.loads(repval.decode("ascii", errors='ignore')))
                                    failedjson = 0
                                    break
                                except:
                                    print("Still could not force into json even after dropping %s" % f)
                            if failedjson == 1:
                                if loadedenv['debug'] == 1:
                                     print(repval.decode("ascii", errors='ignore'))
                                failedjson = 2

                    if loadedenv['debug'] >= 1 and failedjson >= 1:
                        print ("JSON Error - Debug - Attempting to print")
                        print("Raw form kafka:")
                        try:
                            print(message.value())
                        except:
                            print("Raw message failed to print")
                        print("Ascii Decoded (Sent to json.dumps):")
                        try:
                            print(val)
                        except:
                            print("Ascii dump message failed to print")

        elif message.error().code() != KafkaError._PARTITION_EOF:
            print("MyError: " + message.error())
            running = False
            break


            # If our row count is over the max, our size is over the max, or time delta is over the max, write the group to the parquet.
        if (rowcnt >= loadedenv['rowmax'] or timedelta >= loadedenv['timemax'] or sizecnt >= loadedenv['sizemax']) and len(parqar) > 0:
            parqdf = pd.DataFrame.from_records([l for l in parqar])
            parts = parqdf[loadedenv['partition_field']].unique()
            if loadedenv['debug'] >= 1:
                print("Write Dataframe to %s at %s records - Size: %s - Seconds since last write: %s - Partitions in this batch: %s" % (curfile, rowcnt, sizecnt, timedelta, parts))

            for part in parts:
                partdf =  parqdf[parqdf[loadedenv['partition_field']] == part]
                if loadedenv['write_live'] == 1:
                    base_dir = loadedenv['table_base'] + "/" + part
                else:
                    base_dir = loadedenv['table_base'] + "/" + loadedenv['tmp_part_dir'] + "/" + part

                final_file = base_dir + "/" + curfile
                if not os.path.isdir(base_dir):
                    try:
                        os.makedirs(base_dir)
                    except:
                        print("Partition Create failed, it may have been already created for %s" % (base_dir))
                if loadedenv['debug'] >= 1:
                    print("Writing partition %s to %s" % (part, final_file))
                if not os.path.exists(final_file):
                    parqwrite(final_file, partdf, compression=loadedenv['parq_compress'], row_group_offsets=loadedenv['parq_offsets'], has_nulls=loadedenv['has_nulls'])
                else:
                    parqwrite(final_file, partdf, compression=loadedenv['parq_compress'], row_group_offsets=loadedenv['parq_offsets'], has_nulls=loadedenv['has_nulls'], append=True)
                cursize =  os.path.getsize(final_file)
                ledger = [curtime, cursize, final_file]
                part_ledger[part] = ledger
                partdf = pd.DataFrame()
            parqar = []
            rowcnt = 0
            sizecnt = 0
            lastwrite = curtime


        removekeys = []
        for x in part_ledger.keys():
            l = part_ledger[x][0]
            s = part_ledger[x][1]
            f = part_ledger[x][2]
            base_dir = loadedenv['table_base'] + '/' + x
            if not os.path.isdir(base_dir):
                try:
                    os.makedirs(base_dir)
                except:
                    print("Partition Create failed, it may have been already created for %s" % (base_dir))
            if s > loadedenv['filemaxsize'] or (curtime - l) > loadedenv['partmaxage']:
                new_file_name = loadedenv['uniq_val'] + "_" + str(curtime) + ".parq"
                new_file = base_dir + "/" + new_file_name
                if loadedenv['debug'] >= 1:
                    print("Max Size or Max Age reached - Size: %s - Age: %s - Writing to %s" % (cursize, curtime - l, new_file))
                shutil.move(f, new_file)
                removekeys.append(x)
                # If merge_file is 1 then we read in the whole parquet file and output it in one go to eliminate all the row groups from appending
                if loadedenv['merge_file'] == 1:
                    if loadedenv['debug'] >= 1:
                       print("Merging parqfile into to new parq file")
                    inparq = ParquetFile(new_file)
                    inparqdf = inparq.to_pandas()
                    tmp_file = loadedenv['tmp_part'] + "/" + new_file_name
                    parqwrite(tmp_file, inparqdf, compression=loadedenv['parq_compress'], row_group_offsets=loadedenv['parq_offsets'], has_nulls=loadedenv['has_nulls'])
                    shutil.move(tmp_file, new_file)
                    inparq = None
                    inparqdf = None
        for y in removekeys:
            del part_ledger[y]

    c.close()

def loadenv(evars):
    print("Loading Environment Variables")
    lenv = {}
    for e in evars:
        try:
            val = os.environ[e.upper()]
        except:
            if evars[e][1] == True:
                print("ENV Variable %s is required and not provided - Exiting" % (e.upper()))
                sys.exit(1)
            else:
                print("ENV Variable %s not found, but not required, using default of '%s'" % (e.upper(), evars[e][0]))
                val = evars[e][0]
        if evars[e][2] == 'int':
            val = int(val)
        if evars[e][2] == 'flt':
            val = float(val)
        if evars[e][2] == 'bool':
            val=bool(val)
        lenv[e] = val


    return lenv


# Get our bootstrap string from zookeepers if provided
def bootstrap_from_zk(ZKs, kafka_id):
    from kazoo.client import KazooClient
    zk = KazooClient(hosts=ZKs,read_only=True)
    zk.start()

    brokers = zk.get_children('/%s/brokers/ids' % kafka_id)
    BSs = ""
    for x in brokers:
        res = zk.get('/%s/brokers/ids/%s' % (kafka_id, x))
        dj = json.loads(res[0].decode('utf-8'))
        srv = "%s:%s" % (dj['host'], dj['port'])
        if BSs == "":
            BSs = srv
        else:
            BSs = BSs + "," + srv

    zk.stop()

    zk = None
    return BSs



if __name__ == "__main__":
    main()
