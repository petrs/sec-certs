import csv
import json
import re
import os
import operator

import subprocess
from multiprocessing import Pool, RLock
from multiprocessing.pool import ThreadPool
from multiprocessing.spawn import freeze_support
from re import Pattern
from typing import Sequence
from itertools import islice

from tqdm import tqdm
from enum import Enum
from pathlib import Path
import matplotlib.pyplot as plt
from PyPDF2 import PdfFileReader
from graphviz import Digraph

from . import sanity
from .analyze_certificates import is_in_dict
from .cert_rules import rules, fips_rules, REGEXEC_SEP
from .files import search_files, load_cert_html_file, FILE_ERRORS_STRATEGY
from .constants import *


import sec_certs.constants as constants
import sec_certs.helpers as helpers

plt.rcdefaults()

# if True, then exception is raised when unexpect intermediate number is obtained
# Used as sanity check during development to detect sudden drop in number of extracted features
#APPEND_DETAILED_MATCH_MATCHES = True
APPEND_DETAILED_MATCH_MATCHES = False
VERBOSE = False

LINE_SEPARATOR = ' '
# LINE_SEPARATOR = ''  # if newline is not replaced with space, long string included in matches are found


def get_line_number(lines, line_length_compensation, match_start_index):
    line_chars_offset = 0
    line_number = 1
    for line in lines:
        line_chars_offset += len(line) + line_length_compensation
        if line_chars_offset > match_start_index:
            # we found the line
            return line_number
        line_number += 1
    # not found
    return -1


def get_files_to_process(walk_dir: Path, required_extension: str):
    files_to_process = []
    for file_name in search_files(walk_dir):
        if not os.path.isfile(file_name):
            continue
        file_ext = file_name[file_name.rfind('.'):]
        if file_ext.lower() != required_extension:
            continue
        files_to_process.append(file_name)

    return files_to_process


def print_missing_pdftotext_converts(walk_dir: str):
    items = get_files_to_process(walk_dir, '.pdf')
    missing_txt_converts = []
    for file_name in items:
        file_name_txt = file_name[:file_name.rfind('.')] + '.txt'
        if not os.path.exists(file_name_txt):
            missing_txt_converts.append(file_name)
    print(f'Missing PDF2TXT coversions: from {len(items)} missing {len(missing_txt_converts)}: {missing_txt_converts}')


def convert_pdf_files(walk_dir: Path, num_threads: int, options: Sequence[str]) -> Sequence[subprocess.CompletedProcess]:
    def convert_pdf_file(file_name: str):
        return subprocess.run(["pdftotext", *options, file_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    items = get_files_to_process(walk_dir, '.pdf')

    print('***CONVERT PDF FILES***')
    results = []
    with tqdm(total=len(items)) as progress:
        for result in ThreadPool(num_threads).imap(convert_pdf_file, items):
            progress.update(1)
            results.append(result)
    return results


def load_cert_file(file_name, limit_max_lines=-1, line_separator=LINE_SEPARATOR):
    lines = []
    was_unicode_decode_error = False
    with open(file_name, 'r', errors=FILE_ERRORS_STRATEGY) as f:
        try:
            lines = f.readlines()
        except UnicodeDecodeError:
            f.close()
            was_unicode_decode_error = True
            print('  WARNING: UnicodeDecodeError, opening as utf8')

            with open(file_name, encoding="utf8", errors=FILE_ERRORS_STRATEGY) as f2:
                # coding failure, try line by line
                line = ' '
                while line:
                    try:
                        line = f2.readline()
                        lines.append(line)
                    except UnicodeDecodeError:
                        # ignore error
                        continue

    whole_text = ''
    whole_text_with_newlines = ''
    # we will estimate the line for searched matches
    # => we need to known how much lines were modified (removal of eoln..)
    # for removed newline and for any added separator
    line_length_compensation = 1 - len(LINE_SEPARATOR)
    lines_included = 0
    for line in lines:
        if limit_max_lines != -1 and lines_included >= limit_max_lines:
            break

        whole_text_with_newlines += line
        line = line.replace('\n', '')
        whole_text += line
        whole_text += line_separator
        lines_included += 1

    return whole_text, whole_text_with_newlines, was_unicode_decode_error


def normalize_match_string(match):
    # normalize match
    match = match.strip()
    match = match.rstrip(']')
    match = match.rstrip(os.sep)
    match = match.rstrip(';')
    match = match.rstrip('.')
    match = match.rstrip('”')
    match = match.rstrip('"')
    match = match.rstrip(':')
    match = match.rstrip(')')
    match = match.rstrip('(')
    match = match.rstrip(',')
    match = match.replace('  ', ' ')  # two spaces into one

    sanitized = ''.join(filter(str.isprintable, match))

    return sanitized


def set_match_string(items, key_name, new_value):
    if key_name not in items.keys():
        items[key_name] = new_value
    else:
        old_value = items[key_name]
        if old_value != new_value:
            print('  WARNING: values mismatch, key=\'{}\', old=\'{}\', new=\'{}\''.format(
                key_name, old_value, new_value))


def parse_cert_file(file_name, search_rules, limit_max_lines=-1, line_separator=LINE_SEPARATOR):
    whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
        file_name, limit_max_lines, line_separator)

    # apply all rules
    items_found_all = {}
    for rule_group in search_rules.keys():
        if rule_group not in items_found_all:
            items_found_all[rule_group] = {}

        items_found = items_found_all[rule_group]

        for rule in search_rules[rule_group]:
            if type(rule) != str:
                rule_str = rule.pattern
                #rule_and_sep = re.compile(rule.pattern + REGEXEC_SEP)
                rule_and_sep = rule  # if someone provided already precompiled regex, leave it as it is
            else:
                rule_str = rule
                rule_and_sep = rule + REGEXEC_SEP

            #matches_with_newlines_count = sum(1 for _ in re.finditer(rule_and_sep, whole_text_with_newlines))
            #matches_without_newlines_count = sum(1 for _ in re.finditer(rule_and_sep, whole_text))
            #for m in re.finditer(rule_and_sep, whole_text_with_newlines):
            for m in re.finditer(rule_and_sep, whole_text):
                # insert rule if at least one match for it was found
                if rule_str not in items_found:
                    items_found[rule_str] = {}

                match = m.group()
                match = normalize_match_string(match)

                MAX_ALLOWED_MATCH_LENGTH = 300
                match_len = len(match)
                if match_len > MAX_ALLOWED_MATCH_LENGTH:
                    print('WARNING: Excessive match with length of {} detected for rule {}'.format(match_len, rule))

                if match not in items_found[rule_str]:
                    items_found[rule_str][match] = {}
                    items_found[rule_str][match][TAG_MATCH_COUNTER] = 0
                    if APPEND_DETAILED_MATCH_MATCHES:
                        items_found[rule_str][match][TAG_MATCH_MATCHES] = []
                    # else:
                    #     items_found[rule_str][match][TAG_MATCH_MATCHES] = ['List of matches positions disabled. Set APPEND_DETAILED_MATCH_MATCHES to True']

                items_found[rule_str][match][TAG_MATCH_COUNTER] += 1
                match_span = m.span()
                # estimate line in original text file
                # line_number = get_line_number(lines, line_length_compensation, match_span[0])
                # start index, end index, line number
                # items_found[rule_str][match][TAG_MATCH_MATCHES].append([match_span[0], match_span[1], line_number])
                if APPEND_DETAILED_MATCH_MATCHES:
                    items_found[rule_str][match][TAG_MATCH_MATCHES].append(
                        [match_span[0], match_span[1]])

    # highlight all found strings (by xxxxx) from the input text and store the rest
    all_matches = []
    for rule_group in items_found_all.keys():
        items_found = items_found_all[rule_group]
        for rule_name in items_found.keys():
            for match in items_found[rule_name]:
                all_matches.append(match)

        # if AES string is removed before AES-128, -128 would be left in text => sort by length first
        # sort before replacement based on the length of match
        all_matches.sort(key=len, reverse=True)
        for match in all_matches:
            whole_text_with_newlines = whole_text_with_newlines.replace(
                match, 'x' * len(match))

    return items_found_all, (whole_text_with_newlines, was_unicode_decode_error)


def print_total_matches_in_files(all_items_found_count):
    sorted_all_items_found_count = sorted(
        all_items_found_count.items(), key=operator.itemgetter(1))
    for file_name_count in sorted_all_items_found_count:
        print('{:03d}: {}'.format(file_name_count[1], file_name_count[0]))


def print_total_found_cert_ids(all_items_found_certid_count):
    sorted_certid_count = sorted(
        all_items_found_certid_count.items(), key=operator.itemgetter(1), reverse=True)
    for file_name_count in sorted_certid_count:
        print('{:03d}: {}'.format(file_name_count[1], file_name_count[0]))


def print_guessed_cert_id(cert_id):
    sorted_cert_id = sorted(cert_id.items(), key=operator.itemgetter(1))
    for double in sorted_cert_id:
        just_file_name = double[0]
        if just_file_name.rfind(os.sep) != -1:
            just_file_name = just_file_name[just_file_name.rfind(os.sep) + 1:]
        print('{:30s}: {}'.format(double[1], just_file_name))


def print_all_results(items_found_all):
    # print results
    for rule_group in items_found_all.keys():
        print(rule_group)
        items_found = items_found_all[rule_group]
        for rule in items_found.keys():
            print('  ' + rule)
            for match in items_found[rule]:
                print('    {}: {}'.format(match, items_found[rule][match]))


def count_num_items_found(items_found_all):
    num_items_found = 0
    for rule_group in items_found_all.keys():
        items_found = items_found_all[rule_group]
        for rule in items_found.keys():
            for match in items_found[rule]:
                num_items_found += 1

    return num_items_found


def estimate_cert_id(frontpage_scan, keywords_scan, file_name):
    # check if cert id was extracted from frontpage (most priority)
    frontpage_cert_id = ''
    if frontpage_scan != None:
        if 'cert_id' in frontpage_scan.keys():
            frontpage_cert_id = frontpage_scan['cert_id']

    keywords_cert_id = ''
    if keywords_scan != None:
        # find certificate ID which is the most common
        num_items_found_certid_group = 0
        max_occurences = 0
        items_found = keywords_scan['rules_cert_id']
        for rule in items_found.keys():
            for match in items_found[rule]:
                num_occurences = items_found[rule][match][TAG_MATCH_COUNTER]
                if num_occurences > max_occurences:
                    max_occurences = num_occurences
                    keywords_cert_id = match
                num_items_found_certid_group += num_occurences
        if VERBOSE:
            print('  -> most frequent cert id: {}, {}x'.format(keywords_cert_id,
                                                               num_items_found_certid_group))

    # try to search for certificate id directly in file name - if found, higher priority
    filename_cert_id = ''
    if file_name != None:
        file_name_no_suff = file_name[:file_name.rfind('.')]
        file_name_no_suff = file_name_no_suff[file_name_no_suff.rfind(
            os.sep) + 1:]
        file_name_no_suff += ' '
        file_name_no_suff = file_name_no_suff.replace('%20', ' ')  # some downloaded files are with ' ' replaced to %20
        for rule in rules['rules_cert_id']:
            matches = re.findall(rule, file_name_no_suff)
            if len(matches) > 0:
                # we found cert id directly in name, take longest match
                # print('  -> cert id found directly in certificate name: {}'.format(matches[0]))
                longest_match = matches[0]
                for match in matches:
                    if len(match) > len(longest_match):
                        longest_match = match
                filename_cert_id = longest_match

    if VERBOSE:
        print('Identified cert ids for {}:'.format(file_name))
        print('  frontpage_cert_id: {}'.format(frontpage_cert_id))
        print('  filename_cert_id: {}'.format(filename_cert_id))
        print('  keywords_cert_id: {}'.format(keywords_cert_id))

    MIN_VALID_CERTID_LENGTH = 5
    MAX_VALID_CERTID_LENGTH = 60
    est_cert_id = ''
    if len(keywords_cert_id) >= MIN_VALID_CERTID_LENGTH and len(keywords_cert_id) <= MAX_VALID_CERTID_LENGTH:
        est_cert_id = keywords_cert_id
    if len(frontpage_cert_id) >= MIN_VALID_CERTID_LENGTH and len(frontpage_cert_id) <= MAX_VALID_CERTID_LENGTH:
        est_cert_id = frontpage_cert_id
    if len(filename_cert_id) >= MIN_VALID_CERTID_LENGTH and len(filename_cert_id) <= MAX_VALID_CERTID_LENGTH:
        est_cert_id = filename_cert_id

    if est_cert_id == '':
        print('WARNING: cannot estimate cert_id from {}'.format(file_name))
        print('  (keywords|front|filename|): {}|{}|{} '.format(keywords_cert_id, frontpage_cert_id, filename_cert_id))
    else:
        if len(est_cert_id) < MIN_VALID_CERTID_LENGTH or len(est_cert_id) > MAX_VALID_CERTID_LENGTH:
            print('WARNING: Possibly incorrect cert_id estimation for {} (is \'{}\')'.format(file_name, est_cert_id))
            print('  (keywords|front|filename|): {}|{}|{} '.format(keywords_cert_id, frontpage_cert_id, filename_cert_id))
    return est_cert_id


def save_modified_cert_file(target_file, modified_cert_file_text, is_unicode_text):
    if is_unicode_text:
        write_file = open(target_file, "w", encoding="utf8", errors="replace")
    else:
        write_file = open(target_file, "w", errors="replace")

    try:
        write_file.write(modified_cert_file_text)
    except UnicodeEncodeError as e:
        print('UnicodeDecodeError while writing file fragments back')
    finally:
        write_file.close()


def process_raw_header(items_found):
    return items_found


def print_specified_property_sorted(section_name, item_name, items_found_all):
    specific_item_values = []
    for file_name in items_found_all.keys():
        if section_name in items_found_all[file_name].keys():
            if item_name in items_found_all[file_name][section_name].keys():
                specific_item_values.append(
                    items_found_all[file_name][item_name])
            else:
                print('WARNING: Item {} not found in file {}'.format(
                    item_name, file_name))

    print('*** Occurrences of *{}* item'.format(item_name))
    sorted_items = sorted(specific_item_values)
    for item in sorted_items:
        print(item)


def search_only_headers_bsi(walk_dir: Path):
    print('BSI HEADER SEARCH')
    LINE_SEPARATOR_STRICT = ' '
    NUM_LINES_TO_INVESTIGATE = 15
    rules_certificate_preface = [
        '(BSI-DSZ-CC-.+?) (?:for|For) (.+?) from (.*)',
        '(BSI-DSZ-CC-.+?) zu (.+?) der (.*)',
    ]

    items_found_all = {}
    items_found = {}
    files_without_match = []
    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            no_match_yet = True
            #
            # Process front page with info: cert_id, certified_item and developer
            #
            whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                file_name, NUM_LINES_TO_INVESTIGATE, LINE_SEPARATOR_STRICT)

            for rule in rules_certificate_preface:
                rule_and_sep = rule + REGEXEC_SEP

                for m in re.finditer(rule_and_sep, whole_text):
                    if no_match_yet:
                        items_found_all[file_name] = {}
                        items_found_all[file_name] = {}
                        items_found = items_found_all[file_name]
                        items_found[TAG_HEADER_MATCH_RULES] = []
                        no_match_yet = False

                    # insert rule if at least one match for it was found
                    if rule not in items_found[TAG_HEADER_MATCH_RULES]:
                        items_found[TAG_HEADER_MATCH_RULES].append(rule)

                    match_groups = m.groups()
                    cert_id = match_groups[0]
                    certified_item = match_groups[1]
                    developer = match_groups[2]

                    FROM_KEYWORD_LIST = [' from ', ' der ']
                    for from_keyword in FROM_KEYWORD_LIST:
                        from_keyword_len = len(from_keyword)
                        if certified_item.find(from_keyword) != -1:
                            print(
                                'string **{}** detected in certified item - shall not be here, fixing...'.format(
                                    from_keyword))
                            certified_item_first = certified_item[:certified_item.find(
                                from_keyword)]
                            developer = certified_item[certified_item.find(
                                from_keyword) + from_keyword_len:]
                            certified_item = certified_item_first
                            continue

                    end_pos = developer.find('\f-')
                    if end_pos == -1:
                        end_pos = developer.find('\fBSI')
                    if end_pos == -1:
                        end_pos = developer.find('Bundesamt')
                    if end_pos != -1:
                        developer = developer[:end_pos]

                    items_found[TAG_CERT_ID] = normalize_match_string(cert_id)
                    items_found[TAG_CERT_ITEM] = normalize_match_string(
                        certified_item)
                    items_found[TAG_DEVELOPER] = normalize_match_string(developer)
                    items_found[TAG_CERT_LAB] = 'BSI'

            #
            # Process page with more detailed certificate info
            # PP Conformance, Functionality, Assurance
            rules_certificate_third = [
                'PP Conformance: (.+)Functionality: (.+)Assurance: (.+)The IT Product identified',
            ]

            whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                file_name)

            for rule in rules_certificate_third:
                rule_and_sep = rule + REGEXEC_SEP

                for m in re.finditer(rule_and_sep, whole_text):
                    # check if previous rules had at least one match
                    if not TAG_CERT_ID in items_found.keys():
                        print('ERROR: front page not found for file: {}'.format(file_name))

                    match_groups = m.groups()
                    ref_protection_profiles = match_groups[0]
                    cc_version = match_groups[1]
                    cc_security_level = match_groups[2]

                    items_found[TAG_REFERENCED_PROTECTION_PROFILES] = normalize_match_string(
                        ref_protection_profiles)
                    items_found[TAG_CC_VERSION] = normalize_match_string(
                        cc_version)
                    items_found[TAG_CC_SECURITY_LEVEL] = normalize_match_string(
                        cc_security_level)

            if no_match_yet:
                files_without_match.append(file_name)

            progress.update(1)

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def search_only_headers_nscib(walk_dir: Path):
    print('NSCIB HEADER SEARCH')
    LINE_SEPARATOR_STRICT = ' '
    NUM_LINES_TO_INVESTIGATE = 60
    items_found_all = {}
    items_found = {}
    files_without_match = []
    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            if 'NSCIB-CC-' in file_name:
                #
                # Process front page with info: cert_id, certified_item and developer
                #
                whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                    file_name, NUM_LINES_TO_INVESTIGATE, LINE_SEPARATOR_STRICT)

                certified_item = ''
                developer = ''
                cert_lab = ''
                cert_id = ''
                sponsor = ''

                lines = whole_text_with_newlines.splitlines()
                no_match_yet = True
                item_offset = -1
                for line_index in range(0, len(lines)):
                    line = lines[line_index]

                    if 'Certification Report' in line:
                        item_offset = line_index + 1
                    if 'Assurance Continuity Maintenance Report' in line:
                        item_offset = line_index + 1

                    SPONSORDEVELOPER_STR = 'Sponsor and developer:'
                    if SPONSORDEVELOPER_STR in line:
                        if no_match_yet:
                            items_found_all[file_name] = {}
                            items_found = items_found_all[file_name]
                            no_match_yet = False

                        # all lines above till 'Certification Report' or 'Assurance Continuity Maintenance Report'
                        certified_item = ''
                        for name_index in range(item_offset, line_index):
                            certified_item += lines[name_index] + ' '
                        developer = line[line.find(SPONSORDEVELOPER_STR) + len(SPONSORDEVELOPER_STR):]

                    SPONSOR_STR = 'Sponsor:'
                    if SPONSOR_STR in line:
                        if no_match_yet:
                            items_found_all[file_name] = {}
                            items_found = items_found_all[file_name]
                            no_match_yet = False

                        # all lines above till 'Certification Report' or 'Assurance Continuity Maintenance Report'
                        certified_item = ''
                        for name_index in range(item_offset, line_index):
                            certified_item += lines[name_index] + ' '
                        sponsor = line[line.find(SPONSOR_STR) + len(SPONSOR_STR):]

                    DEVELOPER_STR = 'Developer:'
                    if DEVELOPER_STR in line:
                        developer = line[line.find(DEVELOPER_STR) + len(DEVELOPER_STR):]

                    CERTLAB_STR = 'Evaluation facility:'
                    if CERTLAB_STR in line:
                        cert_lab = line[line.find(CERTLAB_STR) + len(CERTLAB_STR):]

                    REPORTNUM_STR = 'Report number:'
                    if REPORTNUM_STR in line:
                        cert_id = line[line.find(REPORTNUM_STR) + len(REPORTNUM_STR):]

                if no_match_yet:
                    files_without_match.append(file_name)
                else:
                    items_found[TAG_CERT_ID] = normalize_match_string(cert_id)
                    items_found[TAG_CERT_ITEM] = normalize_match_string(
                        certified_item)
                    items_found[TAG_DEVELOPER] = normalize_match_string(developer)
                    items_found[TAG_CERT_LAB] = cert_lab

            progress.update(1)

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def search_only_headers_nscib_regex_unused(walk_dir: Path):
    print('NSCIB HEADER SEARCH')
    LINE_SEPARATOR_STRICT = ' '
    NUM_LINES_TO_INVESTIGATE = 60
    rules_certificate_preface = [
        r'Certification Report(.*)Sponsor and developer:(.*)Evaluation facility:(.*)Report number:(.*)Report version:(.*)Project number:(.*)Author\(s\):(.*)Date:(.*)Number of pages:(.*)Number of appendices:(.*)Reproduction of this report is authorized provided the report is reproduced in its entirety.',
        r'Assurance Continuity Maintenance Report(.*)Sponsor and developer:(.*)Evaluation facility:(.*)Report number:(.*)Report version:(.*)Project number:(.*)Author\(s\):(.*)Date:(.*)Number of pages:(.*)Number of appendices:(.*)Reproduction of this report is authorized provided the report is reproduced in its entirety.'
    ]
    items_found_all = {}
    items_found = {}
    files_without_match = []
    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            no_match_yet = True
            #
            # Process front page with info: cert_id, certified_item and developer
            #
            whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                file_name, NUM_LINES_TO_INVESTIGATE, LINE_SEPARATOR_STRICT)

            for rule in rules_certificate_preface:
                rule_and_sep = rule + REGEXEC_SEP

                for m in re.finditer(rule_and_sep, whole_text):
                    if no_match_yet:
                        items_found_all[file_name] = {}
                        items_found_all[file_name] = {}
                        items_found = items_found_all[file_name]
                        items_found[TAG_HEADER_MATCH_RULES] = []
                        no_match_yet = False

                    # insert rule if at least one match for it was found
                    if rule not in items_found[TAG_HEADER_MATCH_RULES]:
                        items_found[TAG_HEADER_MATCH_RULES].append(rule)

                    match_groups = m.groups()
                    certified_item = match_groups[0]
                    developer = match_groups[1]
                    cert_lab = match_groups[2]
                    cert_id = match_groups[3]

                    items_found[TAG_CERT_ID] = normalize_match_string(cert_id)
                    items_found[TAG_CERT_ITEM] = normalize_match_string(
                        certified_item)
                    items_found[TAG_DEVELOPER] = normalize_match_string(developer)
                    items_found[TAG_CERT_LAB] = cert_lab

            if no_match_yet:
                files_without_match.append(file_name)

            progress.update(1)

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def search_only_headers_niap_regex_unused(walk_dir: Path):
    print('US NIAP HEADER SEARCH')
    LINE_SEPARATOR_STRICT = ' '
    NUM_LINES_TO_INVESTIGATE = 60
    rules_certificate_preface = [
        'Validation Report(.*)Report Number:(.*)Dated:(.*)Version[:]*(.*)National Institute of Standards',
        'Validation Report(.*)Report Number:(.*)Dated:(.*)National Institute of Standards',
        'Validation Report(.*)Report Number:(.*)Version(.*)National Institute of Standards',
        'Maintenance Update of(.*)Maintenance Report Number:(.*)Date of Activity:(.*)References:',
        'ASSURANCE CONTINUITY MAINTENANCE REPORT FOR (.*)Maintenance Report Number:(.*)Date of Activity:(.*)References:'
    ]
    items_found_all = {}
    items_found = {}
    files_without_match = []
    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            no_match_yet = True
            #
            # Process front page with info: cert_id, certified_item and developer
            #
            whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                file_name, NUM_LINES_TO_INVESTIGATE, LINE_SEPARATOR_STRICT)

            for rule in rules_certificate_preface:
                rule_and_sep = rule + REGEXEC_SEP

                for m in re.finditer(rule_and_sep, whole_text):
                    if no_match_yet:
                        items_found_all[file_name] = {}
                        items_found = items_found_all[file_name]
                        items_found[TAG_HEADER_MATCH_RULES] = []
                        no_match_yet = False

                    # insert rule if at least one match for it was found
                    if rule not in items_found[TAG_HEADER_MATCH_RULES]:
                        items_found[TAG_HEADER_MATCH_RULES].append(rule)

                    match_groups = m.groups()
                    certified_item = match_groups[0]
                    cert_id = match_groups[1]

                    items_found[TAG_CERT_ID] = normalize_match_string(cert_id)
                    items_found[TAG_CERT_ITEM] = normalize_match_string(certified_item)
                    items_found[TAG_CERT_LAB] = 'US NIAP'

            if no_match_yet:
                files_without_match.append(file_name)

            progress.update(1)

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def search_only_headers_canada(walk_dir: Path):
    '''
    Find and extract name and cert id from front page.
    :param walk_dir:
    :return:
    '''
    print('CANADA HEADER SEARCH')
    LINE_SEPARATOR_STRICT = ' '
    NUM_LINES_TO_INVESTIGATE = 20
    items_found_all = {}
    items_found = {}
    files_without_match = []
    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                file_name, NUM_LINES_TO_INVESTIGATE, LINE_SEPARATOR_STRICT)

            certified_item = ''
            cert_id = ''

            lines = whole_text_with_newlines.splitlines()
            no_match_yet = True
            item_offset = -1
            for line_index in range(0, len(lines)):
                line = lines[line_index]
                if 'Government of Canada, Communications Security Establishment' in line:
                    REPORTNUM_STR1 = 'Evaluation number:'
                    REPORTNUM_STR2 = 'Document number:'
                    matched_number_str = ''
                    line_certid = lines[line_index + 1]
                    if line_certid.startswith(REPORTNUM_STR1):
                        matched_number_str = REPORTNUM_STR1
                    if line_certid.startswith(REPORTNUM_STR2):
                        matched_number_str = REPORTNUM_STR2
                    if matched_number_str != '':
                        if no_match_yet:
                            items_found_all[file_name] = {}
                            items_found = items_found_all[file_name]
                            no_match_yet = False

                        cert_id = line_certid[line_certid.find(matched_number_str) + len(matched_number_str):]
                        break

                if 'Government of Canada. This document is the property of the Government of Canada. It shall not be altered,' in line:
                    REPORTNUM_STR = 'Evaluation number:'
                    for offset in range(1, 20):
                        line_certid = lines[line_index + offset]
                        if 'UNCLASSIFIED' in line_certid:
                            if no_match_yet:
                                items_found_all[file_name] = {}
                                items_found = items_found_all[file_name]
                                no_match_yet = False
                            line_certid = lines[line_index + offset - 4]
                            cert_id = line_certid[line_certid.find(REPORTNUM_STR) + len(REPORTNUM_STR):]
                            break
                    if not no_match_yet:
                        break

                if 'UNCLASSIFIED / NON CLASSIFIÉ' in line and 'COMMON CRITERIA CERTIFICATION REPORT' in lines[line_index + 2]:
                    line_certid = lines[line_index + 1]
                    if no_match_yet:
                        items_found_all[file_name] = {}
                        items_found = items_found_all[file_name]
                        no_match_yet = False
                    cert_id = line_certid
                    break

            if no_match_yet:
                files_without_match.append(file_name)
            else:
                items_found[TAG_CERT_ID] = normalize_match_string(cert_id)
                items_found[TAG_CERT_LAB] = 'CANADA'

            progress.update(1)

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def search_only_headers_niap(walk_dir: Path):
    '''
    Find and extrcat ite name and cert id from front page.
    NOTE: will miss several older ill-formated Continuity Maintenance Report reports
    :param walk_dir:
    :return:
    '''
    print('US NIAP HEADER SEARCH')
    LINE_SEPARATOR_STRICT = ' '
    NUM_LINES_TO_INVESTIGATE = 15
    items_found_all = {}
    items_found = {}
    files_without_match = []
    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            if 'st_vid' in file_name:
                #
                # Process front page with info: cert_id, certified_item and developer
                #
                whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                    file_name, NUM_LINES_TO_INVESTIGATE, LINE_SEPARATOR_STRICT)

                certified_item = ''
                cert_id = ''

                lines = whole_text_with_newlines.splitlines()
                no_match_yet = True
                item_offset = -1
                for line_index in range(0, len(lines)):
                    line = lines[line_index]

                    if 'Validation Report' in line:
                        item_offset = line_index + 1

                    REPORTNUM_STR = 'Report Number:'
                    if REPORTNUM_STR in line:
                        if no_match_yet:
                            items_found_all[file_name] = {}
                            items_found = items_found_all[file_name]
                            no_match_yet = False

                        # all lines above till 'Certification Report' or 'Assurance Continuity Maintenance Report'
                        certified_item = ''
                        for name_index in range(item_offset, line_index):
                            certified_item += lines[name_index] + ' '
                        cert_id = line[line.find(REPORTNUM_STR) + len(REPORTNUM_STR):]
                        break

                if no_match_yet:
                    files_without_match.append(file_name)
                else:
                    items_found[TAG_CERT_ID] = normalize_match_string(cert_id)
                    items_found[TAG_CERT_ITEM] = normalize_match_string(certified_item)
                    items_found[TAG_CERT_LAB] = 'US NIAP'

            progress.update(1)

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def search_only_headers_anssi(walk_dir: Path):
    class HEADER_TYPE(Enum):
        HEADER_FULL = 1
        HEADER_MISSING_CERT_ITEM_VERSION = 2
        HEADER_MISSING_PROTECTION_PROFILES = 3
        HEADER_DUPLICITIES = 4

    rules_certificate_preface = [
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.*)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.*)Conformité à un profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeurs(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)()Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur (.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom des produits(.+)Référence/version des produits(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur\(s\)(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom des produits(.+)Référence/version des produits(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur (.+)Centre d\'évaluation(.+)Accords de reconnaissance'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profils de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur\(s\)(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à un profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur\(s\)(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à un profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur (.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à des profils de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeurs(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profils de protection(.+)Critères d\’évaluation et version(.+)Niveau d\’évaluation(.+)Développeurs(.+)Centre d\’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit \(référence/version\)(.+)Nom de la TOE \(référence/version\)(.+)Conformité à un profil de protection(.+)Critères d\’évaluation et version(.+)Niveau d\’évaluation(.+)Développeurs(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur\(s\)(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur\(s\)(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit \(référence/version\)(.+)Nom de la TOE \(référence/version\)(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profils de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),

        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur\(s\)(.+)dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  des profils de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit \(rÃ©fÃ©rence/version\)(.+)Nom de la TOE \(rÃ©fÃ©rence/version\)(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification Report(.+)Nom du produit(.+)Référence/version du produit(.*)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© aux profisl de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centres dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)Version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© aux profils de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur\(s\)(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)Versions du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer (.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer (.+)Evaluation facility(.+)Mutual Recognition Agreements'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developers(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer\(s\)(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification report reference(.+)Products names(.+)Products references(.+)protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developers(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification report reference(.+)Product name \(reference / version\)(.+)TOE name \(reference / version\)(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developers(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL,
         'Certification report reference(.+)TOE name(.+)Product\'s reference/ version(.+)TOE\'s reference/ version(.+)Conformité à un profil de protection(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer (.+)Evaluation facility(.+)Recognition arrangements'),

        # corrupted text (duplicities)
        (HEADER_TYPE.HEADER_DUPLICITIES,
         'RÃ©fÃ©rencce du rapport de d certification n(.+)Nom du p produit(.+)RÃ©fÃ©rencce/version du produit(.+)ConformiitÃ© Ã  un profil de d protection(.+)CritÃ¨res d dâ€™Ã©valuation ett version(.+)Niveau dâ€™â€™Ã©valuation(.+)DÃ©velopp peurs(.+)Centre dâ€™â€™Ã©valuation(.+)Accords d de reconnaisssance applicab bles'),

        # rules without product version
        (HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION,
         'Référence du rapport de certification(.+)Nom et version du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION,
         'Référence du rapport de certification(.+)Nom et version du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur (.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION,
         'Référence du rapport de certification(.+)Nom du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),

        # rules without protection profile
        (HEADER_TYPE.HEADER_MISSING_PROTECTION_PROFILES,
         'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
    ]

    #    rules_certificate_preface = [
    #        (HEADER_TYPE.HEADER_FULL, 'ddddd'),
    #    ]

    # statistics about rules success rate
    num_rules_hits = {}
    for rule in rules_certificate_preface:
        num_rules_hits[rule[1]] = 0

    print('***ANSSI HEADER SEARCH***')
    items_found_all = {}
    files_without_match = []

    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                file_name)

            # for ANSII and DCSSI certificates, front page starts only on third page after 2 newpage signs
            pos = whole_text.find('')
            if pos != -1:
                pos = whole_text.find('', pos)
                if pos != -1:
                    whole_text = whole_text[pos:]

            no_match_yet = True
            other_rule_already_match = False
            other_rule = ''
            rule_index = -1
            for rule in rules_certificate_preface:
                rule_index += 1
                rule_and_sep = rule[1] + REGEXEC_SEP

                for m in re.finditer(rule_and_sep, whole_text):
                    if no_match_yet:
                        items_found_all[file_name] = {}
                        items_found = items_found_all[file_name]
                        items_found[TAG_HEADER_MATCH_RULES] = []
                        no_match_yet = False

                    # insert rule if at least one match for it was found
                    if rule not in items_found[TAG_HEADER_MATCH_RULES]:
                        items_found[TAG_HEADER_MATCH_RULES].append(rule[1])

                    if not other_rule_already_match:
                        other_rule_already_match = True
                        other_rule = rule
                    else:
                        print(
                            'WARNING: multiple rules are matching same certification document: ' + file_name)

                    num_rules_hits[rule[1]] += 1  # add hit to this rule

                    match_groups = m.groups()

                    index_next_item = 0

                    items_found[TAG_CERT_ID] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1

                    items_found[TAG_CERT_ITEM] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1

                    if rule[0] == HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION:
                        items_found[TAG_CERT_ITEM_VERSION] = ''
                    else:
                        items_found[TAG_CERT_ITEM_VERSION] = normalize_match_string(
                            match_groups[index_next_item])
                        index_next_item += 1

                    if rule[0] == HEADER_TYPE.HEADER_MISSING_PROTECTION_PROFILES:
                        items_found[TAG_REFERENCED_PROTECTION_PROFILES] = ''
                    else:
                        items_found[TAG_REFERENCED_PROTECTION_PROFILES] = normalize_match_string(
                            match_groups[index_next_item])
                        index_next_item += 1

                    items_found[TAG_CC_VERSION] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1

                    items_found[TAG_CC_SECURITY_LEVEL] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1

                    items_found[TAG_DEVELOPER] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1

                    items_found[TAG_CERT_LAB] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1

            if no_match_yet:
                files_without_match.append(file_name)

            progress.update(1)

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    if True:
        print('# hits for rule')
        sorted_rules = sorted(num_rules_hits.items(),
                              key=operator.itemgetter(1), reverse=True)
        used_rules = []
        for rule in sorted_rules:
            print('{:4d} : {}'.format(rule[1], rule[0]))
            if rule[1] > 0:
                used_rules.append(rule[0])

    return items_found_all, files_without_match


def extract_certificates_frontpage(walk_dir: Path):
    canada_items_found, canada_files_without_match = search_only_headers_canada(
        walk_dir)
    anssi_items_found, anssi_files_without_match = search_only_headers_anssi(
        walk_dir)
    niap_items_found, niap_files_without_match = search_only_headers_niap(
        walk_dir)
    nscib_items_found, nscib_files_without_match = search_only_headers_nscib(
        walk_dir)
    bsi_items_found, bsi_files_without_match = search_only_headers_bsi(
        walk_dir)

    print('*** Files without detected header')
    files_without_match = list(
        set(anssi_files_without_match) & set(bsi_files_without_match) & set(nscib_files_without_match)
        & set(niap_files_without_match) & set(canada_files_without_match))
    for file_name in files_without_match:
        print(file_name)
    print('Total no hits files: {}'.format(len(files_without_match)))

    items_found_all = {**anssi_items_found, **bsi_items_found, **nscib_items_found,
                       **niap_items_found, **canada_items_found}
    # store results into file with fixed name and also with time appendix

    return items_found_all


def search_pp_only_headers(walk_dir: Path):
    # LINE_SEPARATOR_STRICT = ' '
    # NUM_LINES_TO_INVESTIGATE = 15
    # rules_certificate_preface = [
    #     '(Common Criteria Protection Profile .+)?(BSI-PP-CC-.+?)Federal Office for Information Security',
    #     '(Protection Profile for the .+)?Schutzprofil für das .+?Certification-ID (BSI-CC-PP-[0-9]+?) ',
    #  #  'Protection Profile for the Security Module of a Smart Meter Mini-HSM (Mini-HSM Security Module PP) Schutzprofil für das Sicherheitsmodul des Smart Meter Mini-HSM  Mini-HSM SecMod-PP Version 1.0 – 23 June 2017 Certification-ID BSI-CC-PP-0095  Mini-HSM Security Module PP  Bundesamt für Sicherheit in der Informationstechnik'
    # ]

    class HEADER_TYPE(Enum):
        BSI_TYPE1 = 1
        BSI_TYPE2 = 2

        DCSSI_TYPE1 = 11
        DCSSI_TYPE2 = 12
        FRONT_DCSSI_TYPE3 = 13
        FRONT_DCSSI_TYPE4 = 14
        DCSSI_TYPE5 = 15
        DCSSI_TYPE6 = 16

        ANSSI_TYPE1 = 41
        ANSSI_TYPE2 = 42
        ANSSI_TYPE3 = 43

    rules_pp_third = [
        (HEADER_TYPE.BSI_TYPE1,
         'PP Reference .+?Title (.+)?CC Version (.+)?Assurance Level (.+)?General Status (.+)?Version Number (.+)?Registration (.+)?Keywords (.+)?TOE Overview'),
        (HEADER_TYPE.BSI_TYPE2,
         'PP Reference.+?Title: (.+)?Version: (.+)?Date: (.+)?Authors: (.+)?Registration: (.+)?Certification-ID: (.+)?Evaluation Assurance Level: (.+)?CC Version: (.+)?Keywords: (.+)?Specific Terms'),
        (HEADER_TYPE.ANSSI_TYPE1,
         'PROTECTION PROFILE IDENTIFICATION.+?Title: (.+)?Version: (.+)?Publication date: (.+)?Certified by: (.+)?Sponsor: (.+)?Editor: (.+)?Review Committee: (.+)?This Protection Profile is conformant to the Common Criteria version (.+)?The minimum assurance level for this Protection Profile is (.+)?PROTECTION PROFILE PRESENTATION'),
        (HEADER_TYPE.ANSSI_TYPE2,
         'PP reference.+?Title : (.+)?Version : (.+)?Authors : (.+)?Evaluation Assurance Level : (.+)?Registration : (.+)?Conformant to Version (.+)?of Common Criteria.+?Key words : (.+)?A glossary of terms'),
        (HEADER_TYPE.ANSSI_TYPE3,
         'Introduction.+?Title: (.+)?Identifications: (.+)?Editor: (.+)?Date: (.+)?Version: (.+)?Sponsor: (.+)?CC Version: (.+)? This Protection Profile'),
        (HEADER_TYPE.DCSSI_TYPE1,
         'Protection profile reference[ ]*Title: (.+)?Reference: (.+)?, Version (.+)?, (.+)?Author: (.+)?Context'),
        (HEADER_TYPE.DCSSI_TYPE2,
         'Protection profile reference[ ]*Title: (.+)?Author: (.+)?Version: (.+)?Context'),
        (HEADER_TYPE.FRONT_DCSSI_TYPE3,
         'Direction centrale de la sécurité des systèmes d\’information(.+)?(?:Creation date|Date)[ ]*[:]*(.+)?Reference[ ]*[:]*(.+)?Version[ ]*[:]*(.+)?Courtesy Translation[ ]*Courtesy translation.+?under the reference (DCSSI-PP-[0-9/]+)?\.[ ]*Page'),
        #        (HEADER_TYPE.FRONT_DCSSI_TYPE4,
        #         'Direction centrale de la sécurité des systèmes d\’information(.+)?Date[ ]*:(.+)?Reference[ ]*:(.+)?Version[ ]*:(.+)?Courtesy Translation[ ]*Courtesy translation.+?under the reference (DCSSI-PP-[0-9/]+)?\.[ ]*Page'),
        (HEADER_TYPE.FRONT_DCSSI_TYPE4,
         'Direction centrale de la sÃ©curitÃ© des systÃ¨mes dâ€™information (.+)?(?:Creation date|Date)[ ]*:(.+)?Reference[ ]*:(.+)?Version[ ]*:(.+)?Courtesy Translation[ ]*Courtesy translation.+?under the reference (DCSSI-PP-[0-9/]+)?\.[ ]*Page'),
        # 'Direction centrale de la sÃ©curitÃ© des systÃ¨mes dâ€™information  Time-stamping System Protection Profile  Date  : July 18, 2008  Reference  : PP-SH-CCv3.1  Version  : 1.7  Courtesy Translation  Courtesy translation of the protection profile registered and certified by the French Certification Body under the reference DCSSI-PP-2008/07.  Page'
        (HEADER_TYPE.DCSSI_TYPE5,
         'Protection Profile identification[ ]*Title[ ]*[:]*(.+)?Author[ ]*[:]*(.+)?Version[ ]*[:]*(.+)?,(.+)?Sponsor[ ]*[:]*(.+)?CC version[ ]*[:]*(.+)?(?:Context|Protection Profile introduction)'),
        (HEADER_TYPE.DCSSI_TYPE6,
         'PP reference.+?Title[ ]*:(.+)?Author[ ]*:(.+)?Version[ ]*:(.+)?Date[ ]*:(.+)?Sponsor[ ]*:(.+)?CC version[ ]*:(.+)?This protection profile.+?The evaluation assurance level required by this protection profile is (.+)?specified by the DCSSI qualification process'),
        #        (HEADER_TYPE.DCSSI_TYPE7,
        #         'Protection Profile identification.+?Title[ ]*[:]*(.+)?Author[ ]*[:]*(.+)?Version[ ]*[:]*(.+)?,(.+)?Sponsor[ ]*[:]*(.+)?CC version[ ]*[:]*(.+)?Protection Profile introduction')
    ]
    print("***PP HEADER SEARCH***")
    items_found_all = {}
    files_without_match = []
    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            #
            # Process page with more detailed protection profile info
            # PP Reference

            whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(
                file_name)

            no_match_yet = True
            for rule in rules_pp_third:
                rule_and_sep = rule[1] + REGEXEC_SEP

                for m in re.finditer(rule_and_sep, whole_text):
                    if no_match_yet:
                        items_found_all[file_name] = {}
                        items_found_all[file_name] = {}
                        items_found = items_found_all[file_name]
                        items_found[TAG_HEADER_MATCH_RULES] = []
                        no_match_yet = False

                    # insert rule if at least one match for it was found
                    if rule[1] not in items_found[TAG_HEADER_MATCH_RULES]:
                        items_found[TAG_HEADER_MATCH_RULES].append(rule[1])

                    match_groups = m.groups()
                    index = 0

                    if rule[0] == HEADER_TYPE.BSI_TYPE1:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_VERSION,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_SECURITY_LEVEL,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_GENERAL_STATUS,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_ID,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        keywords = match_groups[index].lstrip('  ')
                        set_match_string(items_found, TAG_KEYWORDS, normalize_match_string(
                            keywords[0:keywords.find('  ')]))
                        index += 1
                        set_match_string(items_found, TAG_PP_AUTHORS, 'BSI')
                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'BSI')

                    if rule[0] == HEADER_TYPE.BSI_TYPE2:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_DATE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_AUTHORS,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_REGISTRATOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_ID,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_SECURITY_LEVEL,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_VERSION,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        keywords = match_groups[index].lstrip('  ')
                        set_match_string(items_found, TAG_KEYWORDS, normalize_match_string(
                            keywords[0:keywords.find('  ')]))
                        index += 1
                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'BSI')

                    if rule[0] == HEADER_TYPE.ANSSI_TYPE1:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_DATE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_REGISTRATOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_SPONSOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_EDITOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_REVIEWER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_VERSION,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        level = match_groups[index].lstrip('  ')
                        set_match_string(items_found, TAG_CC_SECURITY_LEVEL, normalize_match_string(
                            level[0:level.find('  ')]))
                        index += 1

                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'ANSSI')

                    if rule[0] == HEADER_TYPE.ANSSI_TYPE2:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_AUTHORS,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_SECURITY_LEVEL,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_REGISTRATOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_VERSION,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_KEYWORDS,
                                         normalize_match_string(match_groups[index]))
                        index += 1

                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'ANSSI')

                    if rule[0] == HEADER_TYPE.ANSSI_TYPE3:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        # todo: parse if multiple pp ids are present
                        set_match_string(items_found, TAG_PP_ID,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_EDITOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_DATE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_SPONSOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        ccversion = match_groups[index].lstrip('  ')
                        set_match_string(items_found, TAG_CC_VERSION, normalize_match_string(
                            ccversion[0:ccversion.find('  ')]))
                        index += 1

                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'ANSSI')

                    if rule[0] == HEADER_TYPE.DCSSI_TYPE1:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_ID,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_DATE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        author = match_groups[index].lstrip('  ')
                        set_match_string(items_found, TAG_PP_AUTHORS, normalize_match_string(
                            author[0:author.find('  ')]))
                        index += 1

                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'DCSSI')

                    if rule[0] == HEADER_TYPE.DCSSI_TYPE2:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_AUTHORS,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        version = match_groups[index].lstrip('  ')
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER, normalize_match_string(
                            version[0:version.find('  ')]))
                        index += 1

                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'DCSSI')

                    if rule[0] == HEADER_TYPE.FRONT_DCSSI_TYPE3 or rule[0] == HEADER_TYPE.FRONT_DCSSI_TYPE4:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_DATE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_ID,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_ID_REGISTRATOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1

                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'DCSSI')

                    if rule[0] == HEADER_TYPE.DCSSI_TYPE5:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_AUTHORS,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_DATE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_SPONSOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        ccversion = match_groups[index].lstrip('  ')
                        set_match_string(items_found, TAG_CC_VERSION, normalize_match_string(
                            ccversion[0:ccversion.find('  ')]))
                        index += 1

                    if rule[0] == HEADER_TYPE.DCSSI_TYPE6:
                        set_match_string(items_found, TAG_PP_TITLE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_AUTHORS,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_VERSION_NUMBER,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_DATE,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_PP_SPONSOR,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_VERSION,
                                         normalize_match_string(match_groups[index]))
                        index += 1
                        set_match_string(items_found, TAG_CC_SECURITY_LEVEL,
                                         normalize_match_string(match_groups[index]))
                        index += 1

                        set_match_string(
                            items_found, TAG_PP_REGISTRATOR_SIMPLIFIED, 'DCSSI')

            if no_match_yet:
                files_without_match.append(file_name)

            progress.update(1)

    print('\n*** Protection profiles without detected header:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def extract_protectionprofiles_frontpage(walk_dir: Path):
    pp_items_found, pp_files_without_match = search_pp_only_headers(walk_dir)

    print('*** Files without detected protection profiles header')
    for file_name in pp_files_without_match:
        print(file_name)
    print('Total no hits files: {}'.format(len(pp_files_without_match)))

    return pp_items_found


def extract_keywords(params):
    file_name, fragments_dir, file_prefix, rules_to_search = params
    result, modified_cert_file = parse_cert_file(file_name, rules_to_search, -1, LINE_SEPARATOR)

    # save report text with highlighted/replaced matches into \\fragments\\ directory
    save_fragments = True if fragments_dir is not None else False
    if save_fragments:
        base_path = file_name[:file_name.rfind(os.sep)]
        file_name_short = file_name[file_name.rfind(os.sep) + 1:]
        target_file = fragments_dir / file_name_short
        save_modified_cert_file(
            target_file, modified_cert_file[0], modified_cert_file[1])

    return file_name, result


def extract_certificates_keywords_parallel_certid(walk_dir: Path, fragments_dir: Path, file_prefix, rules_to_search,
                                               num_threads: int):
    # find certid matches with position
    matches = extract_certificates_keywords_parallel(walk_dir, fragments_dir, file_prefix, rules_to_search, num_threads)

    CTX_PREV_CHARS = 300
    CTX_NEXT_CHARS = 300
    # extract context for matched items
    for file in matches.keys():
        #with open
        file_name = walk_dir / file
        whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(file_name)

        len_file = len(whole_text)
        for rule in matches[file]['rules_cert_id']:
            for match in matches[file]['rules_cert_id'][rule]:
                matches[file]['rules_cert_id'][rule][match]['matches_text'] = []
                for position in matches[file]['rules_cert_id'][rule][match]['matches']:
                    start_index = position[0] - CTX_PREV_CHARS
                    if start_index < 0: start_index = 0
                    end_index = position[1] + CTX_NEXT_CHARS
                    if end_index >= len_file:
                        end_index = len_file - 1
                    context = whole_text[start_index:end_index]
                    matches[file]['rules_cert_id'][rule][match]['matches_text'].append(context)
    return matches


def extract_certificates_keywords_parallel(walk_dir: Path, fragments_dir: Path, file_prefix, rules_to_search, num_threads: int):
    print("***EXTRACT KEYWORDS***")
    all_items_found = {}

    files_to_process = get_files_to_process(walk_dir, '.txt')
    responses = []

    with tqdm(total=len(files_to_process)) as progress:
        with Pool(num_threads) as p:
            batch_len = num_threads * 4
            params = []
            to_process = 0
            for file_name in files_to_process:
                to_process = to_process + 1
                params.append((file_name, fragments_dir, file_prefix, rules_to_search))

                if len(params) == batch_len or to_process == len(files_to_process):
                    results = p.map(extract_keywords, params)
                    for response in results:
                        file_name = response[0]
                        all_items_found[file_name] = response[1]

                    progress.update(batch_len)
                    params = []

    total_items_found = 0
    for file_name in all_items_found:
        total_items_found += count_num_items_found(all_items_found[file_name])

    PRINT_MATCHES = False
    if PRINT_MATCHES:
        all_matches = []
        for file_name in all_items_found:
            print('*' * 10, "FILENAME:", file_name, '*' * 10)
            for rule_group in all_items_found[file_name].keys():
                items_found = all_items_found[file_name][rule_group]
                for rule in items_found.keys():
                    for match in items_found[rule]:
                        if match not in all_matches:
                            print(match)
                            all_matches.append(match)

        sorted_all_matches = sorted(all_matches)
        #        for match in sorted_all_matches:
        #            print(match)

    # verify total matches found
    print('\nTotal matches found: {}'.format(total_items_found))

    return all_items_found


def extract_certificates_keywords(walk_dir: Path, fragments_dir: Path, file_prefix):
    print("***EXTRACT KEYWORDS***")
    all_items_found = {}
    # cert_id = {}

    files_to_process = get_files_to_process(walk_dir, '.txt')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            # parse certificate, return all matches
            all_items_found[file_name], modified_cert_file = parse_cert_file(
                file_name, rules, -1, LINE_SEPARATOR)

            # try to establish the certificate id of the current certificate
            # cert_id[file_cert_name] = estimate_cert_id(
            #     None, all_items_found[file_cert_name], file_name)

            # save report text with highlighted/replaced matches into \\fragments\\ directory
            base_path = file_name[:file_name.rfind(os.sep)]
            file_name_short = file_name[file_name.rfind(os.sep) + 1:]
            target_file = fragments_dir / file_name_short
            save_modified_cert_file(
                target_file, modified_cert_file[0], modified_cert_file[1])

            progress.update(1)

    # print('\nTotal matches found in separate files:')
    # print_total_matches_in_files(all_items_found_count)

    # print('\nFile name and estimated certificate ID:')
    # print_guessed_cert_id(cert_id)

    # depricated_print_dot_graph_keywordsonly(['rules_cert_id'], all_items_found, cert_id, walk_dir, 'certid_graph_from_keywords.dot', True)

    total_items_found = 0
    for file_name in all_items_found:
        total_items_found += count_num_items_found(all_items_found[file_name])

    PRINT_MATCHES = False
    if PRINT_MATCHES:
        all_matches = []
        for file_name in all_items_found:
            print('*' * 10, "FILENAME:", file_name, '*' * 10)
            for rule_group in all_items_found[file_name].keys():
                items_found = all_items_found[file_name][rule_group]
                for rule in items_found.keys():
                    for match in items_found[rule]:
                        if match not in all_matches:
                            print(match)
                            all_matches.append(match)

    sorted_all_matches = sorted(all_matches)
    #        for match in sorted_all_matches:
    #            print(match)

    # verify total matches found
    print('\nTotal matches found: {}'.format(total_items_found))

    return all_items_found


def extract_pdf(params):
    file_name = params

    item = {}
    item['pdf_file_size_bytes'] = os.path.getsize(file_name)
    try:
        with open(file_name, 'rb') as f:
            pdf = PdfFileReader(f)
            # store additional interesting info
            item['pdf_is_encrypted'] = pdf.getIsEncrypted()
            item['pdf_number_of_pages'] = pdf.getNumPages()

            # extract pdf metadata (as dict) and save it
            info = pdf.getDocumentInfo()
            if info is not None:
                for key in info:
                    item[key] = str(info[key])
    except Exception as e:
        item['error'] = str(e)

    return file_name, item


def extract_certificates_pdfmeta_parallel(walk_dir: Path, file_prefix, num_threads: int):
    all_items_found = {}
    counter = 0

    print("***EXTRACT PDFMETA***")
    files_to_process = get_files_to_process(walk_dir, '.pdf')
    with tqdm(total=len(files_to_process)) as progress:
        with Pool(num_threads) as p:
            batch_len = num_threads * 4
            params = []
            to_process = 0
            for file_name in files_to_process:
                to_process = to_process + 1

                params.append((file_name))

                if len(params) == batch_len or to_process == len(files_to_process):
                    results = p.map(extract_pdf, params)
                    for response in results:
                        file_name = response[0]
                        all_items_found[file_name] = response[1]

                    progress.update(batch_len)
                    params = []

                write_intermediate = False
                if write_intermediate:
                    if counter % 100 == 0:
                        # store results into file with fixed name
                        with open("{}_data_pdfmeta_{}.json".format(file_prefix, counter), "w",
                                  errors=FILE_ERRORS_STRATEGY) as write_file:
                            json.dump(all_items_found, write_file, indent=4, sort_keys=True)
                counter += 1

    return all_items_found


def extract_certificates_pdfmeta(walk_dir: Path, file_prefix, results_dir: Path):
    all_items_found = {}
    counter = 0

    print("***EXTRACT PDFMETA***")
    files_to_process = get_files_to_process(walk_dir, '.pdf')
    with tqdm(total=len(files_to_process)) as progress:
        for file_name in files_to_process:
            #        print('*** {} ***'.format(file_name))

            item = {}
            item['pdf_file_size_bytes'] = os.path.getsize(file_name)
            try:
                with open(file_name, 'rb') as f:
                    pdf = PdfFileReader(f)
                    # store additional interesting info
                    item['pdf_is_encrypted'] = pdf.getIsEncrypted()
                    item['pdf_number_of_pages'] = pdf.getNumPages()

                    # extract pdf metadata (as dict) and save it
                    info = pdf.getDocumentInfo()
                    if info is not None:
                        for key in info:
                            item[key] = str(info[key])
            except Exception as e:
                item['error'] = str(e)

            # test save of the data extracted to prevent error only very later
            # try:
            #     with open("{}_temp.json".format(file_prefix), "w") as write_file:
            #         write_file.write(json.dumps(item, indent=4, sort_keys=True))
            # except Exception:
            #     print('  ERROR: invalid data from pdf')

            all_items_found[file_name] = item

            write_intermediate = False
            if write_intermediate:
                if counter % 100 == 0:
                    # store results into file with fixed name
                    with open("{}_data_pdfmeta_{}.json".format(file_prefix, counter), "w",
                              errors=FILE_ERRORS_STRATEGY) as write_file:
                        json.dump(all_items_found, write_file, indent=4, sort_keys=True)
            counter += 1

            progress.update(1)

    return all_items_found


def extract_file_name_from_url(url):
    file_name = url[url.rfind('/') + 1:]
    file_name = file_name.replace('%20', ' ')
    return file_name


def parse_product_updates(updates_chunk, link_files_updates):
    maintenance_reports = []

    rule_with_maintainance_ST = '.*?([0-9]+?-[0-9]+?-[0-9]+?) (.+?)\<br style=' \
                                '.*?\<a href="(.+?)" title="Maintenance Report' \
                                '.*?\<a href="(.+?)" title="Maintenance ST'
    rule_without_maintainance_ST = '.*?([0-9]+?-[0-9]+?-[0-9]+?) (.+?)\<br style=' \
                                   '.*?\<a href="(.+?)" title="Maintenance Report'
    if updates_chunk.find('Maintenance Report(s)') != -1:
        start_pos = updates_chunk.find('Maintenance Report(s)</div>')
        start_pos = updates_chunk.find('<li>', start_pos)
        while start_pos != -1:
            end_pos = updates_chunk.find('</li>', start_pos)
            report_chunk = updates_chunk[start_pos:end_pos]

            start_pos = updates_chunk.find('<li>', end_pos)

            # decide which search rule to use 1) one that matches also Maintenance ST or 2) without it
            if report_chunk.find('Maintenance ST') != -1:
                rule = rule_with_maintainance_ST
            else:
                rule = rule_without_maintainance_ST

            items_found = {}
            for m in re.finditer(rule, report_chunk):
                match_groups = m.groups()
                index_next_item = 0
                items_found['maintenance_date'] = normalize_match_string(
                    match_groups[index_next_item])
                index_next_item += 1
                items_found['maintenance_item_name'] = normalize_match_string(
                    match_groups[index_next_item])
                index_next_item += 1
                items_found['maintenance_link_cert_report'] = normalize_match_string(
                    match_groups[index_next_item])
                index_next_item += 1
                if len(match_groups) > index_next_item:
                    items_found['maintenance_link_security_target'] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1
                else:
                    items_found['maintenance_link_security_target'] = ""

                cert_file_name = extract_file_name_from_url(
                    items_found['maintenance_link_cert_report'])
                items_found['link_cert_report_file_name'] = cert_file_name
                st_file_name = extract_file_name_from_url(
                    items_found['maintenance_link_security_target'])
                items_found['link_security_target_file_name'] = st_file_name

                link_files_updates.append(
                    (items_found['maintenance_link_cert_report'], cert_file_name,
                     items_found['maintenance_link_security_target'], st_file_name))

            maintenance_reports.append(items_found)

    return maintenance_reports


def parse_security_level(security_level):
    start_pos = security_level.find('<br>')
    eal_level = security_level
    eal_augmented = []
    if start_pos != -1:
        eal_level = normalize_match_string(security_level[:start_pos])
        # some augmented items found
        augm_chunk = security_level[start_pos:]
        augm_chunk += ' '
        # items are in form of <br>AVA_VLA.4 <br>AVA_MSU.3 ...
        rule = '\<br\>(.+?) '

        for m in re.finditer(rule, augm_chunk):
            match_groups = m.groups()
            eal_augmented.append(normalize_match_string(match_groups[0]))

    return eal_level, eal_augmented


def extract_certificates_metadata_html(file_name):
    print("***HTML METADATA***")
    print(file_name)
    items_found_all = {}
    download_files_certs = []
    download_files_updates = []
    #    print('*** {} ***'.format(file_name))

    whole_text = load_cert_html_file(file_name)

    whole_text = whole_text.replace('\n', ' ')
    whole_text = whole_text.replace('&nbsp;', ' ')
    whole_text = whole_text.replace('&amp;', '&')

    # First find end extract chunks between <tr class=""> ... </tr>
    start_pos = whole_text.find('<tfoot class="hilite7"')
    start_pos = whole_text.find('<tr class="', start_pos)

    chunks_found = 0
    chunks_matched = 0

    while start_pos != -1:
        end_pos = whole_text.find('</tr>', start_pos)

        chunk = whole_text[start_pos:end_pos]

        even_start_pos = whole_text.find('<tr class="even">', start_pos + 1)
        odd_start_pos = whole_text.find('<tr class="">', start_pos + 1)

        start_pos = min(even_start_pos, odd_start_pos)

        # skip chunks which are not cert item chunks
        if chunk.find('This list was generated on') != -1:
            continue

        chunks_found += 1

        class HEADER_TYPE(Enum):
            HEADER_FULL = 1
            HEADER_FULL_PRE2021 = 2
            HEADER_MISSING_VENDOR_WEB = 3

        # IMPORTANT: order regexes based on their specificity - the most specific goes first
        rules_cc_html = [
            (HEADER_TYPE.HEADER_FULL,
             '\<tr class=(?:""|"even")\>[ ]+\<td class="b"\>(.+?)\<a name="(.+?)" style=.+?\<!-- \<a href="(.+?)" title="Vendor\'s web site" target="_blank"\>(.+?)</a> -->'
             '.+?\<a href="(.+?)" title="Certification Report:.+?" target="_blank" class="button2"\>[ ]+Certification Report[ ]+\</a\>'
             '.+?\<a href="(.+?)" title="Security Target:.+?" target="_blank" class="button2">[ ]+Security Target[ ]+</a>'
             '.+?\<!-- ------ ------ ------ Product Updates ------ ------ ------ --\>'
             '(.+?)<!-- ------ ------ ------ END Product Updates ------ ------ ------ --\>'
             '.+?\<!--end-product-cell--\>'
             '.+?\<td style="text-align:center"\>\<span title=".+?"\>(.+?)\</span\>\</td\>'
             '.+?\<td style="text-align:center"\>(.*?)\</td\>'
             '[ ]+?\<td>(.+?)\</td\>'),
            (HEADER_TYPE.HEADER_FULL_PRE2021,
             '\<tr class=(?:""|"even")\>[ ]+\<td class="b"\>(.+?)\<a name="(.+?)" style=.+?\<!-- \<a href="(.+?)" title="Vendor\'s web site" target="_blank"\>(.+?)</a> -->'
             '.+?\<a href="(.+?)" title="Certification Report:.+?" target="_blank" class="button2"\>Certification Report\</a\>'
             '.+?\<a href="(.+?)" title="Security Target:.+?" target="_blank" class="button2">Security Target</a>'
             '.+?\<!-- ------ ------ ------ Product Updates ------ ------ ------ --\>'
             '(.+?)<!-- ------ ------ ------ END Product Updates ------ ------ ------ --\>'
             '.+?\<!--end-product-cell--\>'
             '.+?\<td style="text-align:center"\>\<span title=".+?"\>(.+?)\</span\>\</td\>'
             '.+?\<td style="text-align:center"\>(.*?)\</td\>'
             '[ ]+?\<td>(.+?)\</td\>'),
            (HEADER_TYPE.HEADER_MISSING_VENDOR_WEB,
             '\<tr class=(?:""|"even")\>[ ]+\<td class="b"\>(.+?)\<a name="(.+?)" style=.+?'
             '.+?\<a href="(.+?)" title="Certification Report:.+?" target="_blank" class="button2"\>Certification Report\</a\>'
             '.+?\<a href="(.+?)" title="Security Target:.+?" target="_blank" class="button2">Security Target</a>'
             '.+?\<!-- ------ ------ ------ Product Updates ------ ------ ------ --\>'
             '(.+?)<!-- ------ ------ ------ END Product Updates ------ ------ ------ --\>'
             '.+?\<!--end-product-cell--\>'
             '.+?\<td style="text-align:center"\>\<span title=".+?"\>(.+?)\</span\>\</td\>'
             '.+?\<td style="text-align:center"\>(.*?)\</td\>'
             '[ ]+?\<td>(.+?)\</td\>'),
        ]

        no_match_yet = True
        for rule in rules_cc_html:
            if not no_match_yet:
                continue  # search only the first match

            rule_and_sep = rule[1]

            for m in re.finditer(rule_and_sep, chunk):
                if no_match_yet:
                    chunks_matched += 1
                    items_found = {}
                    # items_found_all.append(items_found)
                    items_found[TAG_HEADER_MATCH_RULES] = []
                    no_match_yet = False

                # insert rule if at least one match for it was found
                # if rule not in items_found[TAG_HEADER_MATCH_RULES]:
                # items_found[TAG_HEADER_MATCH_RULES].append(rule[1])

                match_groups = m.groups()

                index_next_item = 0
                items_found['cert_item_name'] = normalize_match_string(
                    match_groups[index_next_item])
                index_next_item += 1
                items_found['cc_cert_item_html_id'] = normalize_match_string(
                    match_groups[index_next_item])
                cert_item_id = items_found['cc_cert_item_html_id']
                index_next_item += 1
                if not rule[0] == HEADER_TYPE.HEADER_MISSING_VENDOR_WEB:
                    items_found['company_site'] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1
                    items_found['company_name'] = normalize_match_string(
                        match_groups[index_next_item])
                    index_next_item += 1
                items_found['link_cert_report'] = normalize_match_string(
                    match_groups[index_next_item])
                cert_file_name = extract_file_name_from_url(
                    items_found['link_cert_report'])
                items_found['link_cert_report_file_name'] = cert_file_name
                index_next_item += 1
                items_found['link_security_target'] = normalize_match_string(
                    match_groups[index_next_item])
                st_file_name = extract_file_name_from_url(
                    items_found['link_security_target'])
                items_found['link_security_target_file_name'] = st_file_name
                download_files_certs.append(
                    (
                        items_found['link_cert_report'], cert_file_name, items_found['link_security_target'],
                        st_file_name))
                index_next_item += 1

                items_found['maintainance_updates'] = parse_product_updates(
                    match_groups[index_next_item], download_files_updates)
                index_next_item += 1

                items_found['date_cert_issued'] = normalize_match_string(
                    match_groups[index_next_item])
                index_next_item += 1
                items_found['date_cert_expiration'] = normalize_match_string(
                    match_groups[index_next_item])
                index_next_item += 1
                cc_security_level = normalize_match_string(
                    match_groups[index_next_item])
                items_found['cc_security_level'], items_found['cc_security_level_augmented'] = parse_security_level(
                    cc_security_level)
                index_next_item += 1

                # prepare unique name for dictionary (file name is not enough as multiple records reference same cert)
                item_unique_name = '{}__{}'.format(
                    cert_file_name, cert_item_id)
                if item_unique_name not in items_found_all.keys():
                    items_found_all[item_unique_name] = {}
                    items_found_all[item_unique_name]['html_scan'] = items_found
                else:
                    print('{} already in'.format(cert_file_name))

                continue  # we are interested only in first match

        if no_match_yet:
            print('No match found in block #{}'.format(chunks_found))

    print('Chunks found: {}, Chunks matched: {}'.format(
        chunks_found, chunks_matched))
    if chunks_found != chunks_matched:
        print('WARNING: not all chunks found were matched')

    return items_found_all, download_files_certs, download_files_updates


def check_if_new_or_same(target_dict, target_key, new_value):
    if target_key in target_dict.keys():
        if target_dict[target_key] != new_value:
            if sanity.STOP_ON_UNEXPECTED_NUMS:
                raise ValueError(
                    'ERROR: Stopping on unexpected intermediate numbers')


def extract_certificates_metadata_csv(file_name):
    print("***CSV METADATA***")
    print(file_name)
    items_found_all = {}
    expected_columns = -1
    with open(file_name, errors=FILE_ERRORS_STRATEGY) as csv_file:
        #        print('*** {} ***'.format(file_name))
        csv_reader = csv.reader(csv_file, delimiter=',')
        line_count = 0
        no_further_maintainance = True
        for row in csv_reader:
            if line_count == 0:
                expected_columns = len(row)
                # print(f'Column names are {", ".join(row)}')
                line_count += 1
            else:
                if no_further_maintainance:
                    items_found = {}
                if len(row) == 0:
                    break
                if len(row) != expected_columns:
                    print(
                        'WARNING: Incorrect number of columns in row {} (likely separator , in item name), going to fix...'.format(
                            line_count))
                    # trying to fix
                    if row[4].find('EAL') == -1:
                        row[1] = row[1] + row[2]  # fix name
                        row.remove(row[2])  # remove second part of name
                    if len(row[11]) > 0:  # test if reassesment is filled
                        if row[13].find('http://') != -1:
                            # name
                            row[11] = row[11] + row[12]
                            row.remove(row[12])

                # check if some maintainance reports are present. If yes, then extract these to list of updates
                if len(row[10]) > 0:
                    no_further_maintainance = False
                else:
                    no_further_maintainance = True

                items_found['raw_csv_line'] = str(row)

                index_next_item = 0
                check_if_new_or_same(
                    items_found, 'cc_category', normalize_match_string(row[index_next_item]))
                items_found['cc_category'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cert_item_name', normalize_match_string(row[index_next_item]))
                items_found['cert_item_name'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_manufacturer', normalize_match_string(row[index_next_item]))
                items_found['cc_manufacturer'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_scheme', normalize_match_string(row[index_next_item]))
                items_found['cc_scheme'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_security_level', normalize_match_string(row[index_next_item]))
                items_found['cc_security_level'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_protection_profiles', normalize_match_string(row[index_next_item]))
                items_found['cc_protection_profiles'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_certification_date', normalize_match_string(row[index_next_item]))
                items_found['cc_certification_date'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_archived_date', normalize_match_string(row[index_next_item]))
                items_found['cc_archived_date'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'link_cert_report', normalize_match_string(row[index_next_item]))
                items_found['link_cert_report'] = normalize_match_string(
                    row[index_next_item])
                link_cert_report = items_found['link_cert_report']

                cert_file_name = extract_file_name_from_url(
                    items_found['link_cert_report'])
                check_if_new_or_same(
                    items_found, 'link_cert_report_file_name', cert_file_name)
                items_found['link_cert_report_file_name'] = cert_file_name
                cert_file_name = items_found['link_cert_report_file_name']
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'link_security_target', normalize_match_string(row[index_next_item]))
                items_found['link_security_target'] = normalize_match_string(
                    row[index_next_item])
                st_file_name = extract_file_name_from_url(
                    items_found['link_security_target'])
                items_found['link_security_target_file_name'] = st_file_name
                index_next_item += 1

                if 'maintainance_updates' not in items_found:
                    items_found['maintainance_updates'] = []

                maintainance = {}
                maintainance['cc_maintainance_date'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                maintainance['cc_maintainance_title'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                maintainance['cc_maintainance_report_link'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                maintainance['cc_maintainance_st_link'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                # add this maintainance to parent item only when not empty
                if len(maintainance['cc_maintainance_title']) > 0:
                    items_found['maintainance_updates'].append(maintainance)

                if no_further_maintainance:
                    # prepare unique name for dictionary (file name is not enough as multiple records reference same cert)
                    cert_file_name = cert_file_name.replace('%20', ' ')
                    item_unique_name = cert_file_name
                    item_unique_name = '{}__{}'.format(
                        cert_file_name, line_count)
                    if item_unique_name not in items_found_all.keys():
                        items_found_all[item_unique_name] = {}
                        items_found_all[item_unique_name]['csv_scan'] = items_found
                    else:
                        print('  ERROR: {} already in'.format(cert_file_name))
                        if sanity.STOP_ON_UNEXPECTED_NUMS:
                            raise ValueError(
                                'ERROR: Stopping as value is not unique')

                line_count += 1

    return items_found_all


def fix_pp_url(original_url):
    # links to pp are incorrect - epfiles instead ppfiles
    if original_url.find('/epfiles/') != -1:
        original_url = original_url.replace('/epfiles/', '/ppfiles/')
    original_url = original_url.replace('http://', 'https://')
    original_url = original_url.replace(':443', '')
    return original_url


def extract_pp_metadata_csv(file_name):
    items_found_all = {}
    download_files_certs = []
    download_files_maintainance = []
    expected_columns = -1
    with open(file_name, errors=FILE_ERRORS_STRATEGY) as csv_file:
        csv_reader = csv.reader(csv_file, delimiter=',')
        line_count = 0
        no_further_maintainance = True
        for row in csv_reader:
            if line_count == 0:
                expected_columns = len(row)
                line_count += 1
            else:
                if no_further_maintainance:
                    items_found = {}
                if len(row) == 0:
                    break
                if len(row) != expected_columns:
                    print(
                        'WARNING: Incorrect number of columns in row {} (likely separator , in item name), going to fix...'.format(
                            line_count))
                    # trying to fix
                    if len(row) == expected_columns + 2:
                        row[9] = row[9] + row[10] + row[11]
                        row[10] = row[12]
                        row[11] = row[13]
                        del row[13]
                        del row[12]

                # check if some maintainance reports (based on presence of maintainance date - row[8]) are present.
                # If yes, then extract these to list of updates
                if len(row[8]) > 0:
                    no_further_maintainance = False
                else:
                    no_further_maintainance = True

                items_found['raw_csv_line'] = str(row)

                index_next_item = 0
                check_if_new_or_same(
                    items_found, 'cc_category', normalize_match_string(row[index_next_item]))
                items_found['cc_category'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_pp_name', normalize_match_string(row[index_next_item]))
                items_found['cc_pp_name'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_pp_version', normalize_match_string(row[index_next_item]))
                items_found['cc_pp_version'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_security_level', normalize_match_string(row[index_next_item]))
                items_found['cc_security_level'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_certification_date', normalize_match_string(row[index_next_item]))
                items_found['cc_certification_date'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'cc_archived_date', normalize_match_string(row[index_next_item]))
                items_found['cc_archived_date'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                check_if_new_or_same(
                    items_found, 'link_pp_report', normalize_match_string(row[index_next_item]))
                items_found['link_pp_report'] = normalize_match_string(
                    row[index_next_item])
                items_found['link_pp_report'] = fix_pp_url(
                    items_found['link_pp_report'])
                index_next_item += 1
                pp_report_file_name = extract_file_name_from_url(
                    items_found['link_pp_report'])
                check_if_new_or_same(
                    items_found, 'link_pp_document', normalize_match_string(row[index_next_item]))
                items_found['link_pp_document'] = normalize_match_string(
                    row[index_next_item])
                items_found['link_pp_document'] = fix_pp_url(
                    items_found['link_pp_document'])
                index_next_item += 1
                pp_document_file_name = extract_file_name_from_url(
                    items_found['link_pp_document'])

                if 'maintainance_updates' not in items_found:
                    items_found['maintainance_updates'] = []

                maintainance = {}
                maintainance['cc_pp_maintainance_date'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                maintainance['cc_pp_maintainance_title'] = normalize_match_string(
                    row[index_next_item])
                index_next_item += 1
                maintainance['cc_maintainance_report_link'] = normalize_match_string(
                    row[index_next_item])
                maintainance['cc_maintainance_report_link'] = fix_pp_url(
                    maintainance['cc_maintainance_report_link'])
                index_next_item += 1

                # add this maintainance to parent item only when not empty
                if len(maintainance['cc_pp_maintainance_title']) > 0:
                    items_found['maintainance_updates'].append(maintainance)

                if no_further_maintainance:
                    # prepare unique name for dictionary (file name is not enough as multiple records reference same cert)
                    pp_document_file_name = pp_document_file_name.replace(
                        '%20', ' ')
                    item_unique_name = pp_document_file_name
                    item_unique_name = '{}__{}'.format(
                        pp_document_file_name, line_count)
                    if item_unique_name not in items_found_all.keys():
                        items_found_all[item_unique_name] = {}
                        items_found_all[item_unique_name]['csv_scan'] = items_found
                    else:
                        print('  ERROR: {} already in'.format(
                            pp_document_file_name))
                        if sanity.STOP_ON_UNEXPECTED_NUMS:
                            raise ValueError(
                                'ERROR: Stopping as value is not unique')

                    # save download links for basic protection profile
                    download_files_certs.append((items_found['link_pp_report'], pp_report_file_name,
                                                 items_found['link_pp_document'], pp_document_file_name))
                    # save download links for maintainance updates protection profile
                    for item in items_found['maintainance_updates']:
                        if item['cc_maintainance_report_link'] != "":
                            pp_maintainainace_file_name = extract_file_name_from_url(
                                item['cc_maintainance_report_link'])
                            download_files_maintainance.append(
                                (item['cc_maintainance_report_link'], pp_maintainainace_file_name))

                line_count += 1

    return items_found_all, download_files_certs, download_files_maintainance


def extract_certificates_html(web_dir: Path):
    file_name = web_dir / 'cc_products_active.html'
    items_found_all_active, certs_active, updates_active = extract_certificates_metadata_html(
        file_name)
    for item in items_found_all_active.keys():
        items_found_all_active[item]['html_scan']['cert_status'] = 'active'

    file_name = web_dir / 'cc_products_archived.html'
    items_found_all_archived, certs_archive, updates_archive = extract_certificates_metadata_html(
        file_name)
    for item in items_found_all_archived.keys():
        items_found_all_archived[item]['html_scan']['cert_status'] = 'archived'

    items_found_all = {**items_found_all_active, **items_found_all_archived}

    return items_found_all, certs_active + certs_archive, updates_active + updates_archive


def extract_certificates_csv(web_dir: Path):
    file_name = web_dir / 'cc_products_active.csv'
    items_found_all_active = extract_certificates_metadata_csv(file_name)
    for item in items_found_all_active.keys():
        items_found_all_active[item]['csv_scan']['cert_status'] = 'active'

    file_name =  web_dir / 'cc_products_archived.csv'
    items_found_all_archived = extract_certificates_metadata_csv(file_name)
    for item in items_found_all_archived.keys():
        items_found_all_archived[item]['csv_scan']['cert_status'] = 'archived'

    items_found_all = {**items_found_all_active, **items_found_all_archived}

    return items_found_all


def extract_protectionprofiles_csv(base_dir: Path):
    file_name = base_dir / 'cc_pp_active.csv'
    items_found_all_active, download_files_pp, download_files_pp_updates = extract_pp_metadata_csv(
        file_name)
    for item in items_found_all_active.keys():
        items_found_all_active[item]['csv_scan']['cert_status'] = 'active'


    file_name = base_dir / 'cc_pp_archived.csv'
    items_found_all_archived, download_files_pp, download_files_pp_updates = extract_pp_metadata_csv(
        file_name)
    for item in items_found_all_archived.keys():
        items_found_all_archived[item]['csv_scan']['cert_status'] = 'archived'

    items_found_all = {**items_found_all_active, **items_found_all_archived}

    return items_found_all


def check_expected_cert_results(all_html, all_csv, all_front, all_keywords, all_pdf_meta):
    # CSV
    sanity.check_certs_min_items_found_csv(len(all_csv))
    # HTML
    sanity.check_certs_min_items_found_html(len(all_html))
    # FRONTPAGE
    sanity.check_certs_min_items_found_frontpage(len(all_front))
    # KEYWORDS
    total_items_found = 0
    for file_name in all_keywords.keys():
        total_items_found += count_num_items_found(all_keywords[file_name])
    sanity.check_certs_min_items_found_keywords(total_items_found)


def check_expected_pp_results(all_html, all_csv, all_front, all_keywords):
    # CSV
    sanity.check_pp_min_items_found_csv(len(all_csv))
    # HTML
    sanity.check_pp_min_items_found_html(len(all_html))
    # FRONTPAGE
    sanity.check_pp_min_items_found_frontpage(len(all_front))
    # KEYWORDS
    total_items_found = 0
    for file_name in all_keywords.keys():
        total_items_found += count_num_items_found(all_keywords[file_name])
    sanity.check_pp_min_items_found_keywords(total_items_found)


def collate_certificates_data(all_html, all_csv, all_front, all_keywords, all_pdf_meta, file_name_key):
    print('\n\nPairing results from different scans ***')

    file_name_to_html_name_mapping = {}
    for long_file_name in all_html.keys():
        short_file_name = long_file_name[long_file_name.rfind(os.sep) + 1:]
        if short_file_name != '':
            file_name_to_html_name_mapping[short_file_name] = long_file_name

    file_name_to_front_name_mapping = {}
    for long_file_name in all_front.keys():
        short_file_name = long_file_name[long_file_name.rfind(os.sep) + 1:]
        if short_file_name != '':
            file_name_to_front_name_mapping[short_file_name] = long_file_name

    file_name_to_keywords_name_mapping = {}
    for long_file_name in all_keywords.keys():
        short_file_name = long_file_name[long_file_name.rfind(os.sep) + 1:]
        if short_file_name != '':
            file_name_to_keywords_name_mapping[short_file_name] = [
                long_file_name, 0]

    file_name_to_pdfmeta_name_mapping = {}
    for long_file_name in all_pdf_meta.keys():
        short_file_name = long_file_name[long_file_name.rfind(os.sep) + 1:]
        if short_file_name != '':
            file_name_to_pdfmeta_name_mapping[short_file_name] = [
                long_file_name, 0]

    files_without_matched_certid = {}
    all_cert_items = all_csv
    # pair html data, csv data, front pages and keywords
    for file_name in all_csv.keys():
        pairing_found = False

        file_name_pdf = file_name[:file_name.rfind('__')]
        file_name_txt = file_name_pdf[:file_name_pdf.rfind('.')] + '.txt'
        # file_name_st = all_csv[file_name]['csv_scan']['link_security_target_file_name']
        if is_in_dict(all_csv, [file_name, 'csv_scan', 'link_security_target']):
            file_name_st = extract_file_name_from_url(
                all_csv[file_name]['csv_scan']['link_security_target'])
            file_name_st_txt = file_name_st[:file_name_st.rfind('.')] + '.txt'
        else:
            file_name_st_txt = 'security_target_which_doesnt_exists'

        # for file_and_id in all_html.keys():
        #     # in items extracted from html, names are in form of 'file_name.pdf__number'
        #     if file_and_id.find(file_name_pdf + '__') != -1:
        if 'processed' not in all_cert_items[file_name].keys():
            all_cert_items[file_name]['processed'] = {}
        pairing_found = True
        frontpage_scan = None
        keywords_scan = None

        if file_name_txt in file_name_to_html_name_mapping.keys():
            all_cert_items[file_name]['html_scan'] = all_html[file_name_to_html_name_mapping[file_name_txt][0]]
            file_name_to_html_name_mapping[file_name_txt][1] = 1  # was paired
        else:
            print('WARNING: Corresponding HTML report not found for CSV item {}'.format(
                file_name))
        if file_name_txt in file_name_to_front_name_mapping.keys():
            all_cert_items[file_name]['frontpage_scan'] = all_front[file_name_to_front_name_mapping[file_name_txt]]
            frontpage_scan = all_front[file_name_to_front_name_mapping[file_name_txt]]
        if file_name_txt in file_name_to_keywords_name_mapping.keys():
            all_cert_items[file_name]['keywords_scan'] = all_keywords[
                file_name_to_keywords_name_mapping[file_name_txt][0]]
            # was paired
            file_name_to_keywords_name_mapping[file_name_txt][1] = 1
            keywords_scan = all_keywords[file_name_to_keywords_name_mapping[file_name_txt][0]]
        if file_name_st_txt in file_name_to_keywords_name_mapping.keys():
            all_cert_items[file_name]['st_keywords_scan'] = all_keywords[
                file_name_to_keywords_name_mapping[file_name_st_txt][0]]
            # was paired
            file_name_to_keywords_name_mapping[file_name_st_txt][1] = 1
        if file_name_pdf in file_name_to_pdfmeta_name_mapping.keys():
            all_cert_items[file_name]['pdfmeta_scan'] = all_pdf_meta[
                file_name_to_pdfmeta_name_mapping[file_name_pdf][0]]
            # was paired
            file_name_to_pdfmeta_name_mapping[file_name_pdf][1] = 1
        else:
            print('ERROR: File {} not found in pdfmeta scan'.format(file_name_pdf))
        est_cert_id = estimate_cert_id(frontpage_scan, keywords_scan, file_name)
        all_cert_items[file_name]['processed']['cert_id'] = est_cert_id
        if est_cert_id == '':
            files_without_matched_certid[file_name] = 0

    # fix value of cert_id in 'processed' section
    # build fixed cert_id mapping
    all_cert_ids = collect_all_certid_references(all_cert_items)
    #all_cert_ids = fix_certid_references(all_cert_ids)
    for file_name in all_csv.keys():
        # fix estimated cert_id using all other references found
        current_certid = all_cert_items[file_name]['processed']['cert_id']
        new_cert_id = fix_certid_reference(current_certid, all_cert_ids)
        if len(current_certid) > 0 and current_certid in all_cert_ids.keys():
            if current_certid != new_cert_id:
                print('cert_id updated from {} to {}'.format(current_certid, new_cert_id))
                all_cert_items[file_name]['processed']['cert_id'] = new_cert_id

    # print
    # pair pairing in maintainance updates
    for file_name in all_csv.keys():
        pairing_found = False

        # process all maintainance updates
        for update in all_cert_items[file_name]['csv_scan']['maintainance_updates']:

            file_name_pdf = extract_file_name_from_url(
                update['cc_maintainance_report_link'])
            file_name_txt = file_name_pdf[:file_name_pdf.rfind('.')] + '.txt'

            if is_in_dict(update, ['cc_maintainance_st_link']):
                file_name_st = extract_file_name_from_url(
                    update['cc_maintainance_st_link'])
                file_name_st_pdf = file_name_st
                file_name_st_txt = ''
                if len(file_name_st) > 0:
                    file_name_st_txt = file_name_st[:file_name_st.rfind(
                        '.')] + '.txt'
            else:
                file_name_st_pdf = 'file_name_which_doesnt_exists'
                file_name_st_txt = 'file_name_which_doesnt_exists'

            for file_and_id in all_keywords.keys():
                file_name_keyword_txt = file_and_id[file_and_id.rfind(
                    os.sep) + 1:]
                # in items extracted from html, names are in form of 'file_name.pdf__number'
                if file_name_keyword_txt == file_name_txt:
                    pairing_found = True
                    if file_name_txt in file_name_to_keywords_name_mapping.keys():
                        update['keywords_scan'] = all_keywords[file_name_to_keywords_name_mapping[file_name_txt][0]]
                        if file_name_to_keywords_name_mapping[file_name_txt][1] == 1:
                            print('WARNING: {} already paired'.format(
                                file_name_to_keywords_name_mapping[file_name_txt][0]))
                        # was paired
                        file_name_to_keywords_name_mapping[file_name_txt][1] = 1

                if file_name_keyword_txt == file_name_st_txt:
                    if file_name_st_txt in file_name_to_keywords_name_mapping.keys():
                        update['st_keywords_scan'] = all_keywords[
                            file_name_to_keywords_name_mapping[file_name_st_txt][0]]
                        if file_name_to_keywords_name_mapping[file_name_st_txt][1] == 1:
                            print('WARNING: {} already paired'.format(
                                file_name_to_keywords_name_mapping[file_name_st_txt][0]))
                        # was paired
                        file_name_to_keywords_name_mapping[file_name_st_txt][1] = 1

            if not pairing_found:
                print('WARNING: Corresponding keywords pairing not found for maintaince item {}'.format(
                    file_name))

            for file_and_id in file_name_to_pdfmeta_name_mapping.keys():
                file_name_pdf = file_and_id[file_and_id.rfind(os.sep) + 1:]
                file_name_pdfmeta_txt = file_name_pdf[:file_name_pdf.rfind(
                    '.')] + '.txt'
                # in items extracted from html, names are in form of 'file_name.pdf__number'
                # BUGBUG: mismatch in character case will result in missing paiing (e.g., st_vid3014-vr.pdf)
                if file_name_pdfmeta_txt == file_name_txt:
                    pairing_found = True
                    if file_name_pdf in file_name_to_pdfmeta_name_mapping.keys():
                        update['pdfmeta_scan'] = all_pdf_meta[file_name_to_pdfmeta_name_mapping[file_name_pdf][0]]
                        if file_name_to_pdfmeta_name_mapping[file_name_pdf][1] == 1:
                            print('WARNING: {} already paired'.format(
                                file_name_to_pdfmeta_name_mapping[file_name_pdf][0]))
                        # was paired
                        file_name_to_pdfmeta_name_mapping[file_name_pdf][1] = 1

                if file_name_pdfmeta_txt == file_name_st_txt:
                    if file_name_st_pdf in file_name_to_pdfmeta_name_mapping.keys():
                        update['st_pdfmeta_scan'] = all_pdf_meta[file_name_to_pdfmeta_name_mapping[file_name_st_pdf][0]]
                        if file_name_to_pdfmeta_name_mapping[file_name_st_pdf][1] == 1:
                            print('WARNING: {} already paired'.format(
                                file_name_to_pdfmeta_name_mapping[file_name_st_pdf][0]))
                        # was paired
                        file_name_to_pdfmeta_name_mapping[file_name_st_pdf][1] = 1

            if not pairing_found:
                print('WARNING: Corresponding pdfmeta pairing not found for maintaince item {}'.format(
                    file_name))

    print('*** Files with keywords extracted, which were NOT matched to any CSV item:')
    for item in file_name_to_keywords_name_mapping:
        if file_name_to_keywords_name_mapping[item][1] == 0:  # not paired
            print('  {}'.format(file_name_to_keywords_name_mapping[item][0]))

    # display all record which were not paired
    print('\n\nRecords with missing pairing of frontpage:')
    num_frontpage_missing = 0
    for item in all_cert_items.keys():
        this_item = all_cert_items[item]
        if 'frontpage_scan' not in this_item.keys():
            print('WARNING: {} no frontpage scan detected'.format(item))
            num_frontpage_missing += 1

    print('\n\nRecords with missing pairing of keywords:')
    num_keywords_missing = 0
    for item in all_cert_items.keys():
        this_item = all_cert_items[item]
        if 'keywords_scan' not in this_item.keys():
            print('WARNING: {} no keywords scan detected'.format(item))
            num_keywords_missing += 1

    print('\n\nRecords with missing pairing of pdfmeta:')
    num_pdfmeta_missing = 0
    for item in all_cert_items.keys():
        this_item = all_cert_items[item]
        if 'pdfmeta_scan' not in this_item.keys():
            print('WARNING: {} no pdfmeta scan detected'.format(item))
            num_pdfmeta_missing += 1

    print('Records without frontpage: {}\nRecords without keywords: {}\nRecords without pdfmeta: {}'.format(
        num_frontpage_missing, num_keywords_missing, num_pdfmeta_missing))

    print('\n\nFiles with missing cert_id estimation:')
    for item in files_without_matched_certid.keys():
        print('No cert_id estimated for {}'.format(item))
    print('Total files with missing cert_id = {}'.format(len(files_without_matched_certid)))

    return all_cert_items


def get_manufacturer_simple_name(long_manufacturer, reduction_list):
    if long_manufacturer in reduction_list:
        return reduction_list[long_manufacturer]
    else:
        return long_manufacturer


def build_pp_id_mapping(all_pp_items):
    # read mapping between protection profile id as used in CSV and
    # key used in pp_data_complete_processed.json
    pp_id_mapping = {}
    for pp in all_pp_items:
        if is_in_dict(all_pp_items[pp], ['pp_analysis', 'separate_profiles']):
            for profile in all_pp_items[pp]['pp_analysis']['separate_profiles']:
                pp_id_mapping[profile['pp_id_csv']] = pp

    return pp_id_mapping


def process_certs_data_manufacturer(all_cert_items, all_pp_items):
    print('\n\n *** Processing manufacturers ***')

    #
    # Process 'cc_manufacturer' CSV field
    # 1. separate multiple manufacturers (',' '-' '/' 'and')
    # 2. map different names of a same manufacturer to the same
    manufacturers = []
    for file_name in all_cert_items.keys():
        cert = all_cert_items[file_name]
        # extract manufacturer
        if is_in_dict(cert, ['csv_scan', 'cc_manufacturer']):
            manufacturer = cert['csv_scan']['cc_manufacturer']

            if manufacturer != '':
                if manufacturer not in manufacturers:
                    manufacturers.append(manufacturer)

    sorted_manufacturers = sorted(manufacturers)
    for manuf in sorted_manufacturers:
        print('{}'.format(manuf))

    print('\n\n')
    mapping_csvmanuf_separated = {}
    for manuf in sorted_manufacturers:
        mapping_csvmanuf_separated[manuf] = []

    for manuf in sorted_manufacturers:
        # Manufacturer can be single, multiple, separated by - / and ,
        # heuristics: if separated candidate manufacturer can be found in original list (
        # => is sole manufacturer on another certificate => assumption of correct separation)
        separators = [',', '/']  # , '/', ',', 'and']
        multiple_manuf_detected = False
        for sep in separators:
            list_manuf = manuf.split(sep)
            for i in range(0, len(list_manuf)):
                list_manuf[i] = list_manuf[i].strip()
            if len(list_manuf) > 1:
                all_separated_exists = True
                for separated_manuf in list_manuf:
                    if separated_manuf in sorted_manufacturers:
                        continue
                    else:
                        print('Problematic separator \'{}\' in {}'.format(sep, manuf))
                        all_separated_exists = False
                        break

                if all_separated_exists:
                    for x in list_manuf:
                        mapping_csvmanuf_separated[manuf].append(x)
                    multiple_manuf_detected = True

        if not multiple_manuf_detected:
            mapping_csvmanuf_separated[manuf].append(manuf)

    print('### Multiple manufactures detected and split:')
    for manuf in mapping_csvmanuf_separated:
        if len(mapping_csvmanuf_separated[manuf]) > 1:
            print('  {}:{}'.format(manuf, mapping_csvmanuf_separated[manuf]))

    manuf_starts = {}
    already_reduced = {}
    for manuf1 in sorted_manufacturers:  # we are processing from the shorter to longer
        if manuf1 == '':
            continue
        for manuf2 in sorted_manufacturers:
            if manuf1 != manuf2:
                if manuf2.startswith(manuf1):
                    print('Potential consolidation of manufacturers: {} vs. {}'.format(
                        manuf1, manuf2))
                    if manuf1 not in manuf_starts:
                        manuf_starts[manuf1] = set()
                    manuf_starts[manuf1].add(manuf2)
                    if manuf2 not in already_reduced:
                        already_reduced[manuf2] = manuf1
                    else:
                        print('  Warning: \'{}\' prefixed by \'{}\' already reduced to \'{}\''.format(
                            manuf2, manuf1, already_reduced[manuf2]))

    # try to find manufacturers with multiple names and draw the map
    dot = Digraph(comment='Manufacturers naming simplifications')
    dot.attr('graph', label='Manufacturers naming simplifications',
             labelloc='t', fontsize='30')
    dot.attr('node', style='filled')
    already_inserted_edges = []
    for file_name in all_cert_items.keys():
        cert = all_cert_items[file_name]
        if is_in_dict(cert, ['csv_scan', 'cc_manufacturer']):
            joint_manufacturer = cert['csv_scan']['cc_manufacturer']
            if joint_manufacturer != '':
                for manuf in mapping_csvmanuf_separated[joint_manufacturer]:
                    simple_manuf = get_manufacturer_simple_name(
                        manuf, already_reduced)
                    if simple_manuf != manuf:
                        edge_name = '{}<->{}'.format(simple_manuf, manuf)
                        if edge_name not in already_inserted_edges:
                            dot.edge(simple_manuf, manuf,
                                     color='orange', style='solid')
                            already_inserted_edges.append(edge_name)

    # plot naming hierarchies
    file_name = 'manufacturer_naming_dependency.dot'
    dot.render(file_name, view=False)
    print('{} pdf rendered'.format(file_name))

    pp_id_mapping = build_pp_id_mapping(all_pp_items)

    # update dist with processed list of manufactures
    all_cert_items_keys = list(all_cert_items.keys())
    for file_name in all_cert_items_keys:
        cert = all_cert_items[file_name]
        # extract manufacturer
        if is_in_dict(cert, ['csv_scan', 'cc_manufacturer']):
            manufacturer = cert['csv_scan']['cc_manufacturer']

            if manufacturer != '':
                if 'processed' not in cert:
                    cert['processed'] = {}

                # insert extracted manufacturers by full name
                cert['processed']['cc_manufacturer_list'] = mapping_csvmanuf_separated[manufacturer]

                # insert extracted manufacturers by simplified name
                simple_manufacturers = []
                for manuf in mapping_csvmanuf_separated[manufacturer]:
                    simple_manufacturers.append(
                        get_manufacturer_simple_name(manuf, already_reduced))

                cert['processed']['cc_manufacturer_simple_list'] = simple_manufacturers
                cert['processed']['cc_manufacturer_simple'] = get_manufacturer_simple_name(
                    manufacturer, already_reduced)

        # extract certification lab
        if is_in_dict(cert, ['frontpage_scan', 'cert_lab']):
            lab = cert['frontpage_scan']['cert_lab']

            if lab != '':
                lab = lab.upper()
                if 'processed' not in cert:
                    cert['processed'] = {}

                # insert extracted lab - only the first words, changed to uppercase, omitting the rest
                pos1 = lab.find(' ')
                if pos1 != -1:
                    cert['processed']['cert_lab'] = lab[:pos1]
                else:
                    cert['processed']['cert_lab'] = lab

        # extract security level EAL
        if is_in_dict(cert, ['csv_scan', 'cc_security_level']):
            level = cert['csv_scan']['cc_security_level']
            level_split = level.split(",")
            if level_split[0] == 'None':
                if len(level_split[0]) > 1:
                    level_split[0] = 'EAL0+'
            # REMOVE 20201016: security level from protection profiles done later
            #if level.find(',') != -1:
            #    level = level[:level.find(',')]  # trim list of augmented items
            #level_out = level_split[0]
            #if level == 'None':
            #    if cert['csv_scan']['cc_protection_profiles'] != '':
            #        level_out = 'Protection Profile'

            cert['processed']['cc_security_level'] = level_split[0]
            cert['processed']['cc_security_level_augments'] = level_split[1:]

        # pair cert with its protection profile(s)
        if is_in_dict(cert, ['csv_scan', 'cc_protection_profiles']):
            pp_id_csv = cert['csv_scan']['cc_protection_profiles']
            if pp_id_csv != '':
                # find corresponding protection profile
                if pp_id_csv in pp_id_mapping.keys() and \
                        pp_id_mapping[pp_id_csv] in all_pp_items.keys():
                    pp = all_pp_items[pp_id_mapping[pp_id_csv]]

                    # security level of certificate will be equal to level of protection profile
                    if is_in_dict(cert, ['processed', 'cc_security_level']):
                        if cert['processed']['cc_security_level'] == 'None':
                            cert['processed']['cc_security_level'] = pp['csv_scan']['cc_security_level']
                    else:
                        cert['processed']['cc_security_level'] = pp['csv_scan']['cc_security_level']
                    if cert['processed']['cc_security_level'] != pp['csv_scan']['cc_security_level']:
                        print('WARNING: {} cc_security_level level already set differently than inferred from PP: {} vs. {}'.format(file_name, cert['processed']['cc_security_level'], pp['csv_scan']['cc_security_level']))

                    # there might be more protection profiles in single file - search the right one
                    for sub_profile in pp['pp_analysis']['separate_profiles']:
                        if sub_profile['pp_id_csv'] == pp_id_csv:
                            cert['processed']['cc_pp_name'] = pp['csv_scan']['cc_pp_name']
                            # set protection profile id as ID extracted from pp pdf (if not found csv is used)
                            cert['processed']['cc_pp_id'] = pp_id_csv
                            if sub_profile['pp_id_legacy'] != '':
                                cert['processed']['cc_pp_id'] = sub_profile['pp_id_legacy']
                            # set path to protection profile file
                            cert['processed']['pp_filename'] = sub_profile['pp_filename']

    return all_cert_items


def collect_all_certid_references(all_cert_items: dict):
    # correct all typos and unfinished certid references
    # idea: collect all found references, try to find largest shared prefix (must be at least xx chars)
    # update dist with processed list of manufactures
    all_cert_ids = {}
    all_cert_items_keys = list(all_cert_items.keys())
    for file_name in all_cert_items_keys:
        cert = all_cert_items[file_name]
        # extract certids
        # from certification report
        if is_in_dict(cert, ['keywords_scan', 'rules_cert_id']):
            cert_ids = cert['keywords_scan']['rules_cert_id']
            for certid_regex in cert_ids.keys():
                for cert_id in cert_ids[certid_regex]:
                    all_cert_ids[cert_id] = cert_id  # add this cert_id into map
        # from security target
        if is_in_dict(cert, ['st_keywords_scan', 'rules_cert_id']):
            st_cert_ids = cert['st_keywords_scan']['rules_cert_id']
            for certid_regex in st_cert_ids.keys():
                for cert_id in st_cert_ids[certid_regex]:
                    all_cert_ids[cert_id] = cert_id  # add this cert_id into map

        # from processed cert_id
        if is_in_dict(cert, ['processed', 'cert_id']):
            cert_id = cert['processed']['cert_id']
            if len(cert_id) > 0:
                all_cert_ids[cert_id] = cert_id  # add this cert_id into map

        # from maintainance updates
        if is_in_dict(cert, ['csv_scan', 'maintainance_updates']):
            for update in cert['csv_scan']['maintainance_updates']:
                if is_in_dict(update, ['keywords_scan', 'rules_cert_id']):
                    for certid_regex in update['keywords_scan']['rules_cert_id'].keys():
                        for cert_id in update['keywords_scan']['rules_cert_id'][certid_regex]:
                            all_cert_ids[cert_id] = cert_id  # add this cert_id into map
                if is_in_dict(update, ['st_keywords_scan', 'rules_cert_id']):
                    for certid_regex in update['st_keywords_scan']['rules_cert_id'].keys():
                        for cert_id in update['st_keywords_scan']['rules_cert_id'][certid_regex]:
                            all_cert_ids[cert_id] = cert_id  # add this cert_id into map

    return all_cert_ids


def fix_certid_reference(cert_id: str, all_cert_ids: dict):
    new_cert_id = cert_id
    new_cert_id = new_cert_id.rstrip()

    # ANSSI
    if cert_id.startswith('ANSS'):
        if new_cert_id.startswith('ANSSi'):  # mistyped ANSSi
            new_cert_id = 'ANSSI' + new_cert_id[4:]
        if len(new_cert_id) > len('ANSSI-CC-0000'):
            if new_cert_id[len('ANSSI-CC-0000')] == '_':  # _ instead of / after year (ANSSI-CC-2010_40 -> ANSSI-CC-2010/40)
                new_cert_id = new_cert_id[:len('ANSSI-CC-0000')] + '/' + new_cert_id[len('ANSSI-CC-0000') + 1:]
        if '_' in new_cert_id:  # _ instead of -
            new_cert_id = new_cert_id.replace('_', '-')

    # BSI
    if cert_id.startswith('BSI-DSZ-CC-'):  # unfinished BSI-DSZ-CC
        # missing year
        # lowercase version
        bsi_parts = cert_id.split('-')
        cert_num = None
        cert_version = None
        cert_year = None

        if len(bsi_parts) > 3:
            cert_num = bsi_parts[3]
        if len(bsi_parts) > 4:
            if bsi_parts[4].startswith('V') or bsi_parts[4].startswith('v'):
                cert_version = bsi_parts[4].upper()  # get version in uppercase
            else:
                cert_year = bsi_parts[4]
        if len(bsi_parts) > 5:
            cert_year = bsi_parts[5]

        # year may be missing - try to find the right one
        if cert_year is None:
            for year in range(1996, 2030):
                cert_id_possible = cert_id + '-' + str(year)
                if cert_id_possible in all_cert_ids:
                    # we found version with year
                    cert_year = str(year)
                    break

        # reconstruct BSI number again
        new_cert_id = 'BSI-DSZ-CC'
        if cert_num is not None:
            if len(cert_num) < 4:  # add ommited zero in certnum
                cert_num = '0' + cert_num
            new_cert_id += '-' + cert_num
        if cert_version is not None:
            new_cert_id += '-' + cert_version
        if cert_year is not None:
            new_cert_id += '-' + cert_year

    # Spain
    # 2014-55-INF-1505 v1 -> 2014-55-INF-1505
    if '-INF-' in cert_id:
        # Spain id has incremental version for reassesment/recertificaton, but there is also changing base id => drop version
        spain_parts = cert_id.split('-')
        cert_year = None
        cert_batch = None
        cert_num = None
        cert_version = None
        cert_year = spain_parts[0]
        cert_batch = spain_parts[1]
        cert_num = spain_parts[3]

        if 'v' in cert_num:
            cert_version = cert_num[cert_num.find('v') + 1:]
            cert_num = cert_num[:cert_num.find('v')]
        if 'V' in cert_num:
            cert_version = cert_num[cert_num.find('V') + 1:]
            cert_num = cert_num[:cert_num.find('V')]

        new_cert_id = '{}-{}-INF-{}'.format(cert_year, cert_batch, cert_num)  # drop version

    # Italia
    # OCSI/CERT/ATS/01/2018 -> OCSI/CERT/ATS/01/2018/RC
    if 'OCSI/CERT' in cert_id:
        if not cert_id.endswith('/RC'):
            new_cert_id = cert_id + '/RC'

    return new_cert_id


def fix_certid_references(all_cert_ids: dict):
    # try to fix certificates
    all_ids_keys = list(all_cert_ids.keys())
    for cert_id in all_ids_keys:
        new_cert_id = fix_certid_reference(cert_id, all_cert_ids)

        # update fixed id
        if cert_id != new_cert_id:
            all_cert_ids[cert_id] = new_cert_id

    return all_cert_ids

def process_certs_data_normalize_certid(all_cert_items, all_pp_items):
    print('\n\n *** Process referenced cert ids ***')
    all_cert_ids = collect_all_certid_references(all_cert_items)
    print(len(all_cert_ids))

    for id in sorted(all_cert_ids.keys()):
        print(id)

    # fix heuristically found references
    all_cert_ids = fix_certid_references(all_cert_ids)

    # print updated cert_id refs
    for id in sorted(all_cert_ids.keys()):
        if id != all_cert_ids[id]:
            print('Update detected from {} to {}'.format(id, all_cert_ids[id]))

    # apply fixed references
    all_cert_items_keys = list(all_cert_items.keys())
    reference_type = 'unknown'
    for file_name in all_cert_items_keys:
        cert = all_cert_items[file_name]
        if not is_in_dict(cert, ['processed']):
            cert['processed'] = {}
        if not is_in_dict(cert['processed'], ['referenced_cert_ids']):
            cert['processed']['referenced_cert_ids'] = {}
        if not is_in_dict(cert['processed']['referenced_cert_ids'], [reference_type]):
            cert['processed']['referenced_cert_ids'][reference_type] = {}

        def update_ref_cert_id(cert: dict, matched_ref_cert_ids: dict, sanitized_cert_ids: dict):
            for certid_regex in matched_ref_cert_ids.keys():
                for cert_id in matched_ref_cert_ids[certid_regex]:
                    if sanitized_cert_ids[cert_id] not in cert['processed']['referenced_cert_ids'][reference_type]:
                        cert['processed']['referenced_cert_ids'][reference_type][sanitized_cert_ids[cert_id]] = {}
                        cert['processed']['referenced_cert_ids'][reference_type][sanitized_cert_ids[cert_id]]['count'] = 0
                    ref_count = matched_ref_cert_ids[certid_regex][cert_id]['count']
                    cert['processed']['referenced_cert_ids'][reference_type][sanitized_cert_ids[cert_id]]['count'] += ref_count

        # from cert report
        if is_in_dict(cert, ['keywords_scan', 'rules_cert_id']):
            update_ref_cert_id(cert, cert['keywords_scan']['rules_cert_id'], all_cert_ids)
        # from security target
        if is_in_dict(cert, ['st_keywords_scan', 'rules_cert_id']):
            update_ref_cert_id(cert, cert['st_keywords_scan']['rules_cert_id'], all_cert_ids)
        # from manintaince updates
        if is_in_dict(cert, ['csv_scan', 'maintainance_updates']):
            for update in cert['csv_scan']['maintainance_updates']:
                if is_in_dict(update, ['keywords_scan', 'rules_cert_id']):
                    update_ref_cert_id(cert, update['keywords_scan']['rules_cert_id'], all_cert_ids)
                if is_in_dict(update, ['st_keywords_scan', 'rules_cert_id']):
                    update_ref_cert_id(cert, update['st_keywords_scan']['rules_cert_id'], all_cert_ids)

    # draw versions of certid with revisions
    # BSI-DSZ-CC-id-version-year_maintainance  (e.g., BSI-DSZ-CC-0857-V2-2015_M01)

    return all_cert_items


def process_certificates_data(all_cert_items, all_pp_items):
    print('\n\n *** Extracting useful info from collated files ***')

    all_cert_items = process_certs_data_normalize_certid(all_cert_items, all_pp_items)

    all_cert_items = process_certs_data_manufacturer(all_cert_items, all_pp_items)


    return all_cert_items


def do_extract_cpe_items(results_dir: Path):
    cpe_items = {}
    with open(results_dir / 'official-cpe-dictionary_v2.3.xml', 'r', errors=FILE_ERRORS_STRATEGY) as f:
        lines = f.readlines()
        line_index = 0
        while line_index < len(lines):
            line = lines[line_index]
            if line.startswith('  <cpe-item'):
                line = lines[line_index]
                NAME_STR = 'name="'
                name = line[line.find(NAME_STR) + len(NAME_STR): line.find('">')]
                product_info = name.split(':')

                line_index += 1
                line = lines[line_index]
                TITLE_STR = '<title xml:lang="en-US">'
                title = line[line.find(TITLE_STR) + len(TITLE_STR): line.find('</title>')]

                # try to find cpe2.3 item till the ned of element
                cpe_23 = ''
                while True:
                    line_index += 1
                    line = lines[line_index]
                    if line.find('</cpe-item>') != -1:
                        break
                    CPE32_ITEM = '<cpe-23:cpe23-item name="'
                    if line.find(CPE32_ITEM) != -1:
                        cpe_23 = line[line.find(CPE32_ITEM) + len(CPE32_ITEM) : line.find('"/>')]
                        break

                cpe_item = {}
                #cpe_item['id'] = name
                cpe_item['vendor'] = product_info[2]
                cpe_item['product'] = product_info[3]
                cpe_item['version'] = product_info[4]
                cpe_item['title'] = title
                cpe_item['cpe-23'] = cpe_23

                cpe_items[name] = cpe_item

            line_index += 1

    return cpe_items


def find_string_tokenized(target_string: str, find_string: str, split_token: str):
    all_match = True
    for word in find_string.split(split_token):  # sentinel_license_manager -> ['sentinel', 'license', 'manager']
        if target_string.find(word) == -1:
            all_match = False
            break

    return all_match

def replace_escaped_chars_dict(target_dict: dict):
    for key in target_dict.keys():
        target_dict[key] = replace_escaped_chars(target_dict[key])
    return target_dict


def replace_escaped_chars(target_string: str):
    pos = target_string.find('%')
    while pos != -1:
        out_string = target_string[:pos]
        num_str = target_string[pos+1:pos+3]
        try:
            num = int(num_str, 16)
            out_string += chr(num)
            out_string += target_string[pos + 3:]
        except ValueError as e:
            print(e)
            out_string += target_string[pos + 1:]

        target_string = out_string
        pos = target_string.find('%')

    return target_string


def do_process_cpe_to_certs(params):
    cpe_to_process, all_cert_items, num_threads, display_progress = params

    progress = None
    if display_progress:
        progress = tqdm(total=len(cpe_to_process.keys()) * num_threads)  # assume roughly same number of items per thread

    certs_to_cpe = {}
    cpe_to_certs = {}
    for cpe_item_key in cpe_to_process.keys():
        cpe_item = cpe_to_process[cpe_item_key]
        cpe_vendor_lower = cpe_item['vendor'].lower()
        cpe_vendor_lower = replace_escaped_chars(cpe_vendor_lower)
        cpe_version_lower = cpe_item['version'].lower()
        cpe_product_lower = cpe_item['product'].lower()
        cpe_product_lower = replace_escaped_chars(cpe_product_lower)
        for cert_key in all_cert_items.keys():
            cert = all_cert_items[cert_key]
            # first match vendor
            cert_manuf_lower = cert['csv_scan']['cc_manufacturer'].lower()
            if find_string_tokenized(cert_manuf_lower, cpe_vendor_lower, '_'):
                # vendor is matching, try to match version
                cert_name_lower = cert['csv_scan']['cert_item_name'].lower()
                # try to find all words from the cpe product separately in the certificate name
                # sentinel_license_manager -> ['sentinel', 'license', 'manager']
                all_match = find_string_tokenized(cert_name_lower, cpe_product_lower, '_')

                # check if version match (or is not provided)
                #if not (cpe_version_lower == '-' or cert_name_lower.find(cpe_version_lower) != -1):
                if cpe_version_lower == '-' or cpe_version_lower == '':  # ignore items without version
                    all_match = False
                else:
                    if cert_name_lower.find(cpe_version_lower) == -1:
                        all_match = False

                if all_match:
                    print('possible match: {}  =  {}'.format(cert['csv_scan']['cert_item_name'], cpe_item_key))
                    certs_to_cpe[cert_key] = certs_to_cpe.get(cert_key, [])
                    certs_to_cpe[cert_key].append(cpe_item_key)
                    cpe_to_certs[cpe_item_key] = cpe_to_certs.get(cpe_item_key, [])
                    cpe_to_certs[cpe_item_key].append(cert_key)

                    # TODO: try to match also title content

        if progress:
            progress.update(1 * num_threads)    # assume that other threads are running same speed and we just processed one item

    return certs_to_cpe, cpe_to_certs


def do_process_cpe_to_certs_parallel(cpe_items: dict, all_cert_items: dict, num_threads: int):
    all_certs_to_cpe = {}
    all_cpe_to_certs = {}
    with tqdm(total=len(cpe_items.keys())) as progress:
        with Pool(num_threads) as p:
            batch_len = num_threads * 4
            num_items_per_thread = 1000
            cpe_to_process = {}
            to_process = 0
            params = []
            for cpe_item_key in cpe_items.keys():
                to_process = to_process + 1
                cpe_to_process[cpe_item_key] = cpe_items[cpe_item_key]
                if len(cpe_to_process) == num_items_per_thread:
                    params.append((cpe_to_process, all_cert_items))
                    cpe_to_process = {}
                if len(params) == batch_len or to_process == len(cpe_items.keys()):
                    results = p.map(do_process_cpe_to_certs, params)
                    #results = p.map(do_process_cpe_to_certs, (cpe_to_process, all_cert_items))
                    for response in results:
                        certs_to_cpe = response[0]
                        cpe_to_certs = response[1]

                        for cert_key in certs_to_cpe.keys():
                            all_certs_to_cpe[cert_key] = all_certs_to_cpe.get(cert_key, [])
                            all_certs_to_cpe[cert_key].append(certs_to_cpe[cert_key])

                        for cpe_item_key_local in cpe_to_certs.keys():
                            all_cpe_to_certs[cpe_item_key_local] = all_cpe_to_certs.get(cpe_item_key_local, [])
                            all_cpe_to_certs[cpe_item_key_local].append(cpe_to_certs[cpe_item_key_local])

                    progress.update(batch_len * num_items_per_thread)
                    params = []

    return all_certs_to_cpe, all_cpe_to_certs


def do_process_cpe_to_certs_parallel2(cpe_items: dict, all_cert_items: dict, num_threads: int):
    def chunks(data, SIZE=10000):
        it = iter(data)
        for i in range(0, len(data), SIZE):
            yield {k: data[k] for k in islice(it, SIZE)}


    all_certs_to_cpe = {}
    all_cpe_to_certs = {}
    with Pool(num_threads) as p:
        # split cpe_items into num_threads parts
        params = []
        display_progress = True
        for cpe_to_process in chunks(cpe_items, round(len(cpe_items) / num_threads)):
            params.append((cpe_to_process, all_cert_items, num_threads, display_progress))
            display_progress = False  # display progress only for the first threat

        results = p.map(do_process_cpe_to_certs, params)
        for response in results:
            certs_to_cpe = response[0]
            cpe_to_certs = response[1]

            for cert_key in certs_to_cpe.keys():
                all_certs_to_cpe[cert_key] = all_certs_to_cpe.get(cert_key, [])
                all_certs_to_cpe[cert_key] += certs_to_cpe[cert_key]

            for cpe_item_key_local in cpe_to_certs.keys():
                all_cpe_to_certs[cpe_item_key_local] = all_cpe_to_certs.get(cpe_item_key_local, [])
                all_cpe_to_certs[cpe_item_key_local] += cpe_to_certs[cpe_item_key_local]

    return all_certs_to_cpe, all_cpe_to_certs