from bson.objectid import ObjectId
from config.constants import API_TIME_FORMAT, IDENTIFIERS,\
    WIFI, CALL_LOG, LOG_FILE, CHUNK_TIMESLICE_QUANTUM, HUMAN_READABLE_TIME_LABEL,\
    VOICE_RECORDING, TEXTS_LOG, SURVEY_TIMINGS, SURVEY_ANSWERS, POWER_STATE,\
    BLUETOOTH, ACCELEROMETER, GPS, CONCURRENT_NETWORK_OPS, CHUNKS_FOLDER,\
    CHUNKABLE_FILES, FILE_PROCESS_PAGE_SIZE, ALL_DATA_STREAMS, data_stream_to_s3_file_name_string
from cronutils.error_handler import ErrorHandler
from datetime import datetime

from db.data_access_models import ChunksRegistry, FileToProcess, FilesToProcess, ChunksRegistry, ChunkRegistry, FileProcessLock
from db.study_models import Studies
from libs.s3 import s3_list_files, s3_delete, s3_retrieve, s3_upload
from multiprocessing.pool import ThreadPool
from collections import defaultdict, deque
import gc

def reindex_all_files_to_process():
    """ Totally removes the FilesToProcess DB, deletes all chunked files on s3,
    clears the chunksregistry, and then adds all relevent files on s3 to the
    files to process registry. """
    FileProcessLock.lock()
    print str(datetime.now()), "purging FilesToProcess:", FilesToProcess.count()
    FileToProcess.db().drop()
    print str(datetime.now()), "purging existing ChunksRegistry", ChunksRegistry.count()
    ChunkRegistry.db().drop()
    
    pool = ThreadPool(20)
    
    print str(datetime.now()), "deleting older chunked data:",
    CHUNKED_DATA = s3_list_files(CHUNKS_FOLDER)
    print len(CHUNKED_DATA)
    pool.map(s3_delete, CHUNKED_DATA)
    del CHUNKED_DATA
    
    print str(datetime.now()), "pulling new files to process..."
    files_lists = pool.map(s3_list_files, [str(s._id) for s in Studies()] )
    print "putting new files to process..."
    for i,l in enumerate(files_lists):
        print str(datetime.now()), i+1, "of", str(Studies.count()) + ",", len(l), "files"
        for fp in l:
            if ".csv" == fp[-4:] or ".mp4" == fp[-4:]:
                FileToProcess.append_file_for_processing(fp, ObjectId(fp.split("/", 1)[0]), fp.split("/", 2)[1])
    del files_lists, l
    pool.close()
    pool.terminate()
    print str(datetime.now()), "processing data."
    FileProcessLock.unlock()
    process_file_chunks()

def reindex_specific_data_type(data_type):
    #TODO: this function has only been tested with survey timings.
    print "starting..."
    #this line will raise an error if something is wrong with the data type
    file_name_key = data_stream_to_s3_file_name_string(data_type)
    relevant_chunks = ChunksRegistry(data_type=data_type)
    relevant_indexed_files = [ chunk["chunk_path"] for chunk in relevant_chunks ]
    print "purging old data..."
    for chunk in relevant_chunks: chunk.remove()

    pool = ThreadPool(20)
    pool.map(s3_delete, relevant_indexed_files)

    print "pulling files to process..."
    files_lists = pool.map(s3_list_files, [str(s._id) for s in Studies()] )
    print "parsing files to process..."
    for i,l in enumerate(files_lists):
        print str(datetime.now()), i+1, "of", str(Studies.count()) + ",", len(l), "files"
        for fp in l:
            if file_name_key in fp and (".csv" == fp[-4:] or ".mp4" == fp[-4:]):
                FileToProcess.append_file_for_processing(fp, ObjectId(fp.split("/", 1)[0]), fp.split("/", 2)[1])
    del files_lists, l
    pool.close()
    pool.terminate()
    print str(datetime.now()), "processing data..."
    process_file_chunks()
    print "Done."


"""########################## Hourly Update Tasks ###########################"""

def process_file_chunks():
    """ This is the function that is called from cron.  It runs through all new
    files that have been uploaded and 'chunks' them. Handles logic for skipping
    bad files, raising errors appropriately. """ 
    FileProcessLock.lock()
    error_handler = ErrorHandler()
#     error_handler = null_error_handler
    number_bad_files = 0
    run_count = 0
    while True:
        previous_number_bad_files = number_bad_files
        starting_length = FilesToProcess.count()
        print str(datetime.now()), starting_length
        number_bad_files += do_process_file_chunks(FILE_PROCESS_PAGE_SIZE, error_handler, number_bad_files)
        if starting_length == FilesToProcess.count(): #zero files processed
            if previous_number_bad_files == number_bad_files: #every file broke.
                break
            else: continue
    FileProcessLock.unlock()
    print FilesToProcess.count()
    error_handler.raise_errors()

def do_process_file_chunks(count, error_handler, skip_count):
    """
    Run through the files to process, pull their data, put it into s3 bins.
    Run the file through the appropriate logic path based on file type.
    
    If a file is empty put its ftp object to the empty_files_list, we can't
        delete objects in-place while iterating over the db. 
    
    All files except for the audio recording mp4 file are in the form of CSVs,
    most of those files can be separated by "time bin" (separated into one-hour
    chunks) and concatenated and sorted trivially. A few files, call log,
    identifier file, and wifi log, require some triage beforehand.  The debug log
    cannot be correctly sorted by time for all elements, because it was not
    actually expected to be used by researchers, but is apparently quite useful.
    
    Any errors are themselves concatenated using the passed in error handler.
    """
    #this is how you declare a defaultdict containing a tuple of two deques.
    binified_data = defaultdict( lambda : ( deque(), deque() ) )
    ftps_to_remove = set([]);
    pool = ThreadPool(CONCURRENT_NETWORK_OPS)
    for element in pool.map(batch_retrieve_for_processing,
                          FilesToProcess(page_size=count+skip_count)[skip_count:],
                          chunksize=1):
        with error_handler:
            #raise errors that we encountered in the s3 access threaded operations to the error_handler
            if isinstance(element, Exception): raise element
            ftp, data_type, chunkable, file_contents = element
            del element
            s3_file_path = ftp["s3_file_path"]
#             print s3_file_path
            if chunkable:
                newly_binified_data = process_csv_data(ftp["study_id"],
                                     ftp["user_id"], data_type, file_contents,
                                     s3_file_path)
                if newly_binified_data:
                    append_binified_csvs(binified_data, newly_binified_data, ftp)
                else: # delete empty files from FilesToProcess
                    ftps_to_remove.add(ftp._id)
                continue
            else:
                timestamp = clean_java_timecode( s3_file_path.rsplit("/", 1)[-1][:-4])

                ChunkRegistry.add_new_chunk(ftp["study_id"], ftp["user_id"],
                                            data_type, s3_file_path, timestamp)
                ftps_to_remove.add(ftp._id)
    pool.close()
    pool.terminate()
    more_ftps_to_remove, number_bad_files = upload_binified_data(binified_data, error_handler)
    ftps_to_remove.update(more_ftps_to_remove)
    for ftp_id in ftps_to_remove:
        FileToProcess(ftp_id).remove()
    gc.collect()
    return number_bad_files
    
def upload_binified_data(binified_data, error_handler):
    """ Takes in binified csv data and handles uploading/downloading+updating
        older data to/from S3 for each chunk.
        Returns a set of concatenations that have succeeded and can be removed.
        Returns the number of failed FTPS so that we don't retry them.
        Raises any errors on the passed in ErrorHandler."""
    failed_ftps = set([])
    ftps_to_retire = set([])
    upload_these = []
    for binn, values in binified_data.items():
        data_rows_deque, ftp_deque = values
        with error_handler:
            try:
                study_id, user_id, data_type, time_bin, header = binn
                rows = list(data_rows_deque)
                header = convert_unix_to_human_readable_timestamps(header, rows)
                chunk_path = construct_s3_chunk_path(study_id, user_id, data_type, time_bin) 
                chunk = ChunkRegistry(chunk_path=chunk_path)
                if not chunk:
                    ensure_sorted_by_timestamp(rows)
                    new_contents = construct_csv_string(header, rows)
                    upload_these.append((chunk_path, new_contents, study_id))
                    ChunkRegistry.add_new_chunk(study_id, user_id, data_type,
                                    chunk_path,time_bin, file_contents=new_contents )
                else:
                    s3_file_data = s3_retrieve( chunk_path, study_id, raw_path=True )
                    old_header, old_rows = csv_to_list(s3_file_data) 
                    if old_header != header:
#to handle the case where a file was on an hour boundry and placed in two separate
#chunks we need to FAIL to retire this file. If this happens AND ONE of the files
#DOES NOT have a header mismatch this may (will?) cause data duplication in the
#chunked file whenever the file processing occurs run.
                        raise HeaderMismatchException('%s\nvs.\n%s\nin\n%s' %
                                                      (old_header, header, chunk_path) )
                    old_rows.extend(rows)
                    ensure_sorted_by_timestamp(old_rows)
                    new_contents = construct_csv_string(header, old_rows)
                    upload_these.append(( chunk_path, new_contents, study_id ))
                    chunk.update_chunk_hash(new_contents)
            except Exception as e:
                failed_ftps.update(ftp_deque)
                print study_id, user_id, data_type, time_bin, header
                raise e
            ftps_to_retire.update(ftp_deque)
    pool = ThreadPool(CONCURRENT_NETWORK_OPS)
    pool.map(batch_upload, upload_these, chunksize=1)
    pool.close()
    pool.terminate()
    #The things in ftps to removed that are not in failed ftps.
    return ftps_to_retire.difference(failed_ftps), len(failed_ftps)

"""################################ S3 Stuff ################################"""

def construct_s3_chunk_path(study_id, user_id, data_type, time_bin):
    """ S3 file paths for chunks are of this form:
        CHUNKED_DATA/study_id/user_id/data_type/time_bin.csv """
    return "%s/%s/%s/%s/%s.csv" % (CHUNKS_FOLDER, study_id, user_id, data_type,
        unix_time_to_string(time_bin*CHUNK_TIMESLICE_QUANTUM) )

"""################################# Key ####################################"""

def file_path_to_data_type(file_path):
    if "/accel/" in file_path: return ACCELEROMETER
    if "/bluetoothLog/" in file_path: return BLUETOOTH
    if "/callLog/" in file_path: return CALL_LOG
    if "/gps/" in file_path: return GPS
    if "/identifiers" in file_path: return IDENTIFIERS
    if "/logFile/" in file_path: return LOG_FILE
    if "/powerState/" in file_path: return POWER_STATE
    if "/surveyAnswers/" in file_path: return SURVEY_ANSWERS
    if "/surveyTimings/" in file_path: return SURVEY_TIMINGS
    if "/textsLog/" in file_path: return TEXTS_LOG
    if "/voiceRecording" in file_path: return VOICE_RECORDING
    if "/wifiLog/" in file_path: return WIFI
    raise Exception("data type unknown: %s" % file_path)

def ensure_sorted_by_timestamp(l):
    """ According to the docs the sort method on a list is in place and should
        faster, this is how to declare a sort by the first column (timestamp). """
    l.sort(key = lambda x: int(x[0]))

def convert_unix_to_human_readable_timestamps(header, rows):
    """ Adds a new column to the end which is the unix time represented in
    a human readable time format.  Returns an appropriately modified header. """
    for row in rows:
        unix_millisecond = int(row[0])
        time_string = unix_time_to_string(unix_millisecond / 1000 )
        time_string += "." + str( unix_millisecond % 1000 )
        row.insert(1, time_string)
    header = header.split(",")
    header.insert(1, HUMAN_READABLE_TIME_LABEL)
    return ",".join(header)

def binify_from_timecode(unix_ish_time_code_string):
    """ Takes a unix-ish time code (accepts unix millisecond), and returns an
        integer value of the bin it should go in. """
    actually_a_timecode = clean_java_timecode(unix_ish_time_code_string) # clean java time codes...
    return actually_a_timecode / CHUNK_TIMESLICE_QUANTUM #separate into nice, clean hourly chunks!

"""############################## Standard CSVs #############################"""

def binify_csv_rows(rows_list, study_id, user_id, data_type, header):
    """ Assumes a clean csv with element 0 in the rows column as a unix(ish) timestamp.
        Sorts data points into the appropriate bin based on the rounded down hour
        value of the entry's unix(ish) timestamp. (based CHUNK_TIMESLICE_QUANTUM)
        Returns a dict of form {(study_id, user_id, data_type, time_bin, heeader):rows_lists}. """
    ret = defaultdict(deque)
    for row in rows_list:
        ret[(study_id, user_id, data_type,
             binify_from_timecode(row[0]), header)].append(row)
    return ret

def append_binified_csvs(old_binified_rows, new_binified_rows, file_to_process):
    """ Appends binified rows to an existing binified row data structure.
        Should be in-place. """
    for binn, rows in new_binified_rows.items():
        old_binified_rows[binn][0].extend(rows)  #Add data rows
        old_binified_rows[binn][1].append(file_to_process._id)  #add ftp id

def process_csv_data(study_id, user_id, data_type, file_contents, file_path):
    """ Constructs a binified dict of a given list of a csv rows,
        catches csv files with known problems and runs the correct logic.
        Returns None If the csv has no data in it. """
    if data_type == LOG_FILE: file_contents = fix_app_log_file(file_contents, file_path)
    header, csv_rows_list = csv_to_list(file_contents)
    if data_type == CALL_LOG: header = fix_call_log_csv(header, csv_rows_list)
    if data_type == WIFI: header = fix_wifi_csv(header, csv_rows_list, file_path)
    if data_type == IDENTIFIERS: header = fix_identifier_csv(header, csv_rows_list, file_path)
    if data_type == SURVEY_TIMINGS: header = fix_survey_timings(header, csv_rows_list, file_path)
    if csv_rows_list: return binify_csv_rows(csv_rows_list, study_id, user_id, data_type, header)
    else: return None

"""############################ CSV Fixes #####################################"""

def fix_survey_timings(header, rows_list, file_path):
    """ Survey timings need to have a column inserted stating the survey id they come from."""
    survey_id = file_path.rsplit("/", 2)[1]
    for row in rows_list: row.append(", " + survey_id)
    header
    header += u",survey_id"
    return header

def fix_call_log_csv(header, rows_list):
    """ The call log has poorly ordered columns, the first column should always be
        the timestamp, it has it in column 3.
        Note: older versions of the app name the timestamp column "date". """
    for row in rows_list:
        row.insert(0, row.pop(2))
    header_list = header.split(",")
    header_list.insert(0, header_list.pop(2))
    return ",".join(header_list)

def fix_identifier_csv(header, rows_list, file_name):
    """ The identifiers file has its timestamp in the file name. """
    time_stamp = file_name.rsplit("_", 1)[-1][:-4]
    return insert_timestamp_single_row_csv(header, rows_list, time_stamp)

def fix_wifi_csv(header, rows_list, file_name):
    """ Fixing wifi requires inserting the same timestamp on EVERY ROW.
    The wifi file has its timestamp in the filename. """
    time_stamp = file_name.rsplit("/", 1)[-1][:-4]
    for row in rows_list[:-1]: #uhg, the last row is a new line.
        row = row.insert(0, time_stamp)
    rows_list.pop(-1) #remove last row
    return "timestamp," + header

def fix_app_log_file(file_contents, file_path):
    """ The log file is less of a csv than it is a time enumerated list of 
        events, with the time code preceding each row.
        We insert a base value, a new row stating that a new log file was created,
        which allows us to guarantee at least one timestamp in the file."""
    time_stamp = file_path.rsplit("/", 1)[-1][:-4]
    rows_list = file_contents.splitlines()
    rows_list[0] = time_stamp + " New app log file created"
    new_rows = []
    for row in rows_list:
        row_elements = row.split(" ", 1) #split first whitespace, element 0 is a java timecode
        try:
            int(row_elements[0]) #grab first element, check if it is a valid int
            new_rows.append(row_elements)
        except ValueError as e:
            if ("Device does not" == row[:15] or
                "Trying to start Accelerometer" == row[:29] ):
                #use time stamp from previous row
                new_rows.append(new_rows[-1][0] + row)
                continue
            if ("bluetooth Failure" == row[:17] or
                "our not-quite-race-condition" == row[:28] or
                "accelSensorManager" in row[:18] or #this actually covers 2 cases
                "a sessionactivity tried to clear the" == row[:36] ):
                #Just drop matches to the above lines 
                continue
            raise e
    return "timestamp, event\n" + "\n".join(",".join(row) for row in new_rows)

"""###################################### CSV Utils ##################################"""

def insert_timestamp_single_row_csv(header, rows_list, time_stamp):
    """ Inserts the timestamp field into the header of a csv, inserts the timestamp
        value provided into the first column.  Returns the new header string."""
    header_list = header.split(",")
    header_list.insert(0, "timestamp")
    rows_list[0].insert(0, time_stamp)
    return ",".join(header_list)

def csv_to_list(csv_string):
    """ Grab a list elements from of every line in the csv, strips off trailing
        whitespace. dumps them into a new list (of lists), and returns the header
        line along with the list of rows. """
    lines = [ line for line in csv_string.splitlines() ]
    return lines[0], [row.split(",") for row in lines[1:]]

def construct_csv_string(header, rows_list):
    """ Takes a header list and a csv and returns a single string of a csv.
        Now handles unicode errors.  :D :D :D """
    ret = header.decode("utf") + u"\n" + u"\n".join( [u",".join([x.decode("utf") for x in row]) for row in rows_list ] )
    return ret.encode("utf")

def clean_java_timecode(java_time_code_string):
    """ converts millisecond time (string) to an integer normal unix time. """
    return int(java_time_code_string[:10])

def unix_time_to_string(unix_time):
    return datetime.utcfromtimestamp(unix_time).strftime( API_TIME_FORMAT )

""" Batch Operations """
def batch_retrieve_for_processing(ftp):
    """ Used for mapping an s3_retrieve function. """
    data_type = file_path_to_data_type(ftp['s3_file_path'])
    if data_type in CHUNKABLE_FILES:
        #in the event of an error (boto error) we actually RETURN that error and
        # handle it back on the main thread.
        try: return ftp, data_type, True, s3_retrieve(ftp['s3_file_path'], ftp["study_id"], raw_path=True)
        except Exception as e: return e
    else: return ftp, data_type, False, ""
    
def batch_upload(upload):
    """ Used for mapping an s3_upload function. """
    s3_upload(*upload, raw_path=True)

""" Exceptions """
class HeaderMismatchException(Exception): pass

