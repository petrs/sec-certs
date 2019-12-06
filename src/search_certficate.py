import re
import os, sys
import operator
from graphviz import Digraph
from graphviz import Graph
import json
from cert_rules import rules
from time import gmtime, strftime
from shutil import copyfile
from enum import Enum
from collections import defaultdict

STOP_ON_INCORRECT_NUMS = False
APPEND_DETAILED_MATCH_MATCHES = False
VERBOSE = False

REGEXEC_SEP = '[ ,;\]”)(]'
LINE_SEPARATOR = ' '
#LINE_SEPARATOR = ''  # if newline is not replaced with space, long string included in matches are found
TAG_MATCH_COUNTER = 'count'
TAG_MATCH_MATCHES = 'matches'

TAG_CERT_HEADER_PROCESSED = 'cert_header_processed'

TAG_CERT_ID = 'cert_id'
TAG_CC_SECURITY_LEVEL = 'cc_security_level'
TAG_CC_VERSION = 'cc_version'
TAG_CERT_LAB = 'cert_lab'
TAG_CERT_ITEM = 'cert_item'
TAG_CERT_ITEM_VERSION = 'cert_item_version'
TAG_DEVELOPER = 'developer'
TAG_REFERENCED_PROTECTION_PROFILES = 'ref_protection_profiles'
TAG_HEADER_MATCH_RULES = 'match_rules'


def search_files(folder):
    for root, dirs, files in os.walk(folder):
        yield from [os.path.join(root, x) for x in files]


def path_leaf(path):
    head, tail = ntpath.split(path)
    return tail or ntpath.basename(head)


unicode_decode_error = []


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


def load_cert_file(file_name, limit_max_lines=-1, line_separator=LINE_SEPARATOR):
    lines = []
    was_unicode_decode_error = False
    with open(file_name, 'r') as f:
        try:
            lines = f.readlines()
        except UnicodeDecodeError:
            f.close()
            was_unicode_decode_error = True
            print('UnicodeDecodeError')
            unicode_decode_error.append(file_name)

            with open(file_name, encoding="utf8") as f2:
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
    line_length_compensation = 1 - len(LINE_SEPARATOR)  # for removed newline and for any added separator
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


def load_cert_html_file(file_name):
    with open(file_name, 'r') as f:
        try:
            whole_text = f.read()
        except UnicodeDecodeError:
            f.close()
            with open(file_name, encoding="utf8") as f2:
                whole_text = f2.read()

    return whole_text


def normalize_match_string(match):
    # normalize match
    match = match.strip()
    match = match.rstrip(']')
    match = match.rstrip('/')
    match = match.rstrip(';')
    match = match.rstrip('.')
    match = match.rstrip('”')
    match = match.rstrip('"')
    match = match.rstrip(':')
    match = match.rstrip(')')
    match = match.rstrip('(')
    match = match.rstrip(',')
    match = match.replace('  ', ' ')  # two spaces into one

    return match


def parse_cert_file(file_name, search_rules, limit_max_lines=-1, line_separator=LINE_SEPARATOR):
    whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(file_name, limit_max_lines, line_separator)

    # apply all rules
    items_found_all = {}
    for rule_group in search_rules.keys():
        if rule_group not in items_found_all:
            items_found_all[rule_group] = {}

        items_found = items_found_all[rule_group]

        for rule in search_rules[rule_group]:
            rule_and_sep = rule + REGEXEC_SEP

            for m in re.finditer(rule_and_sep, whole_text):
                # insert rule if at least one match for it was found
                if rule not in items_found:
                    items_found[rule] = {}

                match = m.group()
                match = normalize_match_string(match)

                if match not in items_found[rule]:
                    items_found[rule][match] = {}
                    items_found[rule][match][TAG_MATCH_COUNTER] = 0
                    if APPEND_DETAILED_MATCH_MATCHES:
                        items_found[rule][match][TAG_MATCH_MATCHES] = []
                    # else:
                    #    items_found[rule][match][TAG_MATCH_MATCHES] = ['List of matches positions disabled. Set APPEND_DETAILED_MATCH_MATCHES to True']

                items_found[rule][match][TAG_MATCH_COUNTER] += 1
                match_span = m.span()
                # estimate line in original text file
                # line_number = get_line_number(lines, line_length_compensation, match_span[0])
                # start index, end index, line number
                #items_found[rule][match][TAG_MATCH_MATCHES].append([match_span[0], match_span[1], line_number])
                if APPEND_DETAILED_MATCH_MATCHES:
                    items_found[rule][match][TAG_MATCH_MATCHES].append([match_span[0], match_span[1]])


    # highlight all found strings from the input text and store the rest
    for rule_group in items_found_all.keys():
        items_found = items_found_all[rule_group]
        for rule in items_found.keys():
            for match in items_found[rule]:
                whole_text_with_newlines = whole_text_with_newlines.replace(match, 'x' * len(match)) # warning - if AES string is removed before AES-128, -128 will be left in text (does it matter?)

    return items_found_all, (whole_text_with_newlines, was_unicode_decode_error)





def print_total_matches_in_files(all_items_found_count):
    sorted_all_items_found_count = sorted(all_items_found_count.items(), key=operator.itemgetter(1))
    for file_name_count in sorted_all_items_found_count:
        print('{:03d}: {}'.format(file_name_count[1], file_name_count[0]))

def print_total_found_cert_ids(all_items_found_certid_count):
    sorted_certid_count = sorted(all_items_found_certid_count.items(), key=operator.itemgetter(1), reverse=True)
    for file_name_count in sorted_certid_count:
        print('{:03d}: {}'.format(file_name_count[1], file_name_count[0]))


def print_guessed_cert_id(cert_id):
    sorted_cert_id = sorted(cert_id.items(), key=operator.itemgetter(1))
    for double in sorted_cert_id:
        just_file_name = double[0]
        if just_file_name.rfind('\\') != -1:
            just_file_name = just_file_name[just_file_name.rfind('\\') + 1:]
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


def print_dot_graph_keywordsonly(filter_rules_group, all_items_found, cert_id, walk_dir, out_dot_name, thick_as_occurences):
    # print dot
    dot = Digraph(comment='Certificate ecosystem: {}'.format(filter_rules_group))
    dot.attr('graph', label='{}'.format(walk_dir), labelloc='t', fontsize='30')
    dot.attr('node', style='filled')

    # insert nodes believed to be cert id for the processed certificates
    for cert in cert_id.keys():
        if cert != "":
            dot.attr('node', color='green')
            dot.node(cert_id[cert])

    dot.attr('node', color='gray')
    for file_name in all_items_found.keys():
        just_file_name = file_name
        this_cert_id = cert_id[file_name]

        if file_name.rfind('\\') != -1:
            just_file_name = file_name[file_name.rfind('\\') + 1:]

        # insert file name and identified probable certification id
        if this_cert_id != "":
            dot.edge(this_cert_id, this_cert_id, label=just_file_name)

        items_found_group = all_items_found[file_name]
        for rules_group in items_found_group.keys():

            # process only specified rule groups
            if rules_group not in filter_rules_group:
                continue

            items_found = items_found_group[rules_group]
            for rule in items_found.keys():
                for match in items_found[rule]:
                    if match != this_cert_id:
                        if thick_as_occurences:
                            num_occurrences = str(items_found[rule][match][TAG_MATCH_COUNTER] / 3 + 1)
                        else:
                            num_occurrences = '1'
                        label = str(items_found[rule][match][TAG_MATCH_COUNTER]) # label with number of occurrences
                        if this_cert_id != "":
                            dot.edge(this_cert_id, match, color='orange', style='solid', label=label, penwidth=num_occurrences)

    # Generate dot graph using GraphViz into pdf
    dot.render(out_dot_name, view=False)
    print('{} pdf rendered'.format(out_dot_name))


def print_dot_graph(filter_rules_group, all_items_found, walk_dir, out_dot_name, thick_as_occurences):
    # print dot
    dot = Digraph(comment='Certificate ecosystem: {}'.format(filter_rules_group))
    dot.attr('graph', label='{}'.format(walk_dir), labelloc='t', fontsize='30')
    dot.attr('node', style='filled')

    # insert nodes believed to be cert id for the processed certificates
    for cert_long_id in all_items_found.keys():
        if defaultdict(lambda: defaultdict(lambda: None), all_items_found[cert_long_id])['processed']['cert_id'] is not None:
            dot.attr('node', color='green')
            dot.node(all_items_found[cert_long_id]['processed']['cert_id'])

    dot.attr('node', color='gray')
    for cert_long_id in all_items_found.keys():
        # do not continue if no keywords were extracted
        if 'keywords_scan' not in all_items_found[cert_long_id].keys():
            continue

        cert = all_items_found[cert_long_id]
        this_cert_id = ''
        if defaultdict(lambda: defaultdict(lambda: None), cert)['processed']['cert_id'] is not None:
            this_cert_id = cert['processed']['cert_id']
        if defaultdict(lambda: defaultdict(lambda: None), cert)['html_scan']['cert_item_name'] is not None:
            this_cert_name = cert['html_scan']['cert_item_name']

        just_file_name = cert['html_scan']['link_cert_report_file_name']

        # insert file name and identified probable certification id
        if this_cert_id != "":
            dot.edge(this_cert_id, this_cert_id, label=just_file_name)

        items_found_group = all_items_found[cert_long_id]['keywords_scan']
        for rules_group in items_found_group.keys():

            # process only specified rule groups
            if rules_group not in filter_rules_group:
                continue

            items_found = items_found_group[rules_group]
            for rule in items_found.keys():
                for match in items_found[rule]:
                    if match != this_cert_id:
                        if thick_as_occurences:
                            num_occurrences = str(items_found[rule][match][TAG_MATCH_COUNTER] / 3 + 1)
                        else:
                            num_occurrences = '1'
                        label = str(items_found[rule][match][TAG_MATCH_COUNTER]) # label with number of occurrences
                        if this_cert_id != "":
                            dot.edge(this_cert_id, match, color='orange', style='solid', label=label, penwidth=num_occurrences)

    # Generate dot graph using GraphViz into pdf
    dot.render(out_dot_name, view=False)
    print('{} pdf rendered'.format(out_dot_name))


def analyze_references_graph(filter_rules_group, all_items_found):
    # build cert_id to item name mapping
    certid_info = {}
    for cert_long_id in all_items_found.keys():
        cert = all_items_found[cert_long_id]
        if defaultdict(lambda: defaultdict(lambda: None), cert)['processed']['cert_id'] is not None:
            if defaultdict(lambda: defaultdict(lambda: None), cert)['frontpage_scan']['cert_item'] is not None:
                this_cert_id = cert['processed']['cert_id']
                if this_cert_id not in certid_info.keys():
                    certid_info[this_cert_id] = {}
                certid_info[this_cert_id]['cert_item'] = cert['frontpage_scan']['cert_item']

    # build list of references
    referenced_by = {}
    for cert_long_id in all_items_found.keys():
        # do not continue if no keywords were extracted ()
        if 'keywords_scan' not in all_items_found[cert_long_id].keys():
            continue

        cert = all_items_found[cert_long_id]
        this_cert_id = ''
        if defaultdict(lambda: defaultdict(lambda: None), cert)['processed']['cert_id'] is not None:
            this_cert_id = cert['processed']['cert_id']

        items_found_group = all_items_found[cert_long_id]['keywords_scan']
        for rules_group in items_found_group.keys():

            # process only specified rule groups
            if rules_group not in filter_rules_group:
                continue

            items_found = items_found_group[rules_group]
            for rule in items_found.keys():
                for match in items_found[rule]:
                    if match != this_cert_id:
                        if this_cert_id != "":
                            # add this_cert_id to the list of references of match item
                            if match not in referenced_by:
                                referenced_by[match] = []
                            if this_cert_id not in referenced_by[match]:
                                referenced_by[match].append(this_cert_id)

    #
    # process direct references
    #
    referenced_by_direct_nums = {}
    for cert_id in referenced_by.keys():
        referenced_by_direct_nums[cert_id] = len(referenced_by[cert_id])

    print('### Certificates sorted by number of other certificates directly referencing them:')
    sorted_ref_direct = sorted(referenced_by_direct_nums.items(), key=operator.itemgetter(1), reverse=False)
    for cert_id in sorted_ref_direct:
        if defaultdict(lambda: defaultdict(lambda: None), certid_info)[cert_id[0]]['cert_item'] is not None:
            print('  {} : {}x directly: {}'.format(cert_id[0], cert_id[1], certid_info[cert_id[0]]['cert_item']))
        else:
            print('  {} : {}x directly'.format(cert_id[0], cert_id[1]))
    print('  Total number of certificates referenced at least once: {}'.format(len(sorted_ref_direct)))

    EXPECTED_CERTS_REFERENCED_ONCE = 937
    if EXPECTED_CERTS_REFERENCED_ONCE != len(sorted_ref_direct):
        print('  ERROR: Different than expected num certificates referenced at least once: {} vs. {}'.format(EXPECTED_CERTS_REFERENCED_ONCE, len(sorted_ref_direct)))
        if STOP_ON_INCORRECT_NUMS:
            print(error_different_than_expected)

    #
    # compute indirect num of references
    #
    referenced_by_indirect = {}
    for cert_id in referenced_by.keys():
        referenced_by_indirect[cert_id] = set()
        for item in referenced_by[cert_id]:
            referenced_by_indirect[cert_id].add(item)

    new_change_detected = True
    while new_change_detected:
        new_change_detected = False

        certids_list = referenced_by.keys()
        for cert_id in certids_list:
            tmp_referenced_by_indirect_nums = referenced_by_indirect[cert_id].copy()
            for referencing in tmp_referenced_by_indirect_nums:
                if referencing in referenced_by.keys():
                    tmp_referencing = referenced_by_indirect[referencing].copy()
                    for in_referencing in tmp_referencing:
                        if in_referencing not in referenced_by_indirect[cert_id]:
                            new_change_detected = True
                            referenced_by_indirect[cert_id].add(in_referencing)

    print('### Certificates sorted by number of other certificates indirectly referencing them:')
    referenced_by_indirect_nums = {}
    for cert_id in referenced_by_indirect.keys():
        referenced_by_indirect_nums[cert_id] = len(referenced_by_indirect[cert_id])

    sorted_ref_indirect = sorted(referenced_by_indirect_nums.items(), key=operator.itemgetter(1), reverse=False)
    for cert_id in sorted_ref_indirect:
        if defaultdict(lambda: defaultdict(lambda: None), certid_info)[cert_id[0]]['cert_item'] is not None:
            print('  {} : {}x indirectly: {}'.format(cert_id[0], cert_id[1], certid_info[cert_id[0]]['cert_item']))
        else:
            print('  {} : {}x indirectly'.format(cert_id[0], cert_id[1]))


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
            print('  -> most frequent cert id: {}, {}x'.format(keywords_cert_id, num_items_found_certid_group))

    # try to search for certificate id directly in file name - if found, higher priority
    filename_cert_id = ''
    if file_name != None:
        file_name_no_suff = file_name[:file_name.rfind('.')]
        file_name_no_suff = file_name_no_suff[file_name_no_suff.rfind('\\') + 1:]
        for rule in rules['rules_cert_id']:
            file_name_no_suff += ' '
            matches = re.findall(rule, file_name_no_suff)
            if len(matches) > 0:
                # we found cert id directly in name
                print('  -> cert id found directly in certificate name: {}'.format(matches[0]))
                filename_cert_id = matches[0]

    if VERBOSE:
        print('Identified cert ids for {}:'.format(file_name))
        print('  frontpage_cert_id: {}'.format(frontpage_cert_id))
        print('  filename_cert_id: {}'.format(filename_cert_id))
        print('  keywords_cert_id: {}'.format(keywords_cert_id))

    if frontpage_cert_id != '':
        return frontpage_cert_id
    if filename_cert_id != '':
        return filename_cert_id
    if keywords_cert_id != '':
        return keywords_cert_id

    return ''


def save_modified_cert_file(target_file, modified_cert_file_text, is_unicode_text):
    write_file = None
    if is_unicode_text:
        write_file = open(target_file, "w", encoding="utf8")
    else:
        write_file = open(target_file, "w")

    try:
        write_file.write(modified_cert_file_text)
    except UnicodeEncodeError as e:
        print(erro_my)
        write_file.close()
        print('UnicodeDecodeError while writing file fragments back')

    write_file.close()


def process_raw_header(items_found):
    return items_found


def print_specified_property_sorted(section_name, item_name, items_found_all):
    specific_item_values = []
    for file_name in items_found_all.keys():
        if section_name in items_found_all[file_name].keys():
            if item_name in items_found_all[file_name][section_name].keys():
                specific_item_values.append(items_found_all[file_name][item_name])
            else:
                print('WARNING: Item {} not found in file {}'.format(item_name, file_name))

    print('*** Occurrences of *{}* item'.format(item_name))
    sorted_items = sorted(specific_item_values)
    for item in sorted_items:
        print(item)


def print_found_properties(items_found_all):
    print_specified_property_sorted(TAG_CERT_ID, items_found_all)
    print_specified_property_sorted(TAG_CERT_ITEM , items_found_all)
    print_specified_property_sorted(TAG_CERT_ITEM_VERSION, items_found_all)
    print_specified_property_sorted(TAG_REFERENCED_PROTECTION_PROFILES, items_found_all)
    print_specified_property_sorted(TAG_CC_VERSION , items_found_all)
    print_specified_property_sorted(TAG_CC_SECURITY_LEVEL, items_found_all)
    print_specified_property_sorted(TAG_DEVELOPER , items_found_all)
    print_specified_property_sorted(TAG_CERT_LAB, items_found_all)


def search_only_headers_bsi(walk_dir):
    LINE_SEPARATOR_STRICT = ' '
    NUM_LINES_TO_INVESTIGATE = 15
    rules_certificate_preface = [
        '(BSI-DSZ-CC-.+?) (?:for|For) (.+?) from (.*)',
        '(BSI-DSZ-CC-.+?) zu (.+?) der (.*)',
    ]

    items_found_all = {}
    files_without_match = []
    for file_name in search_files(walk_dir):
        if not os.path.isfile(file_name):
            continue
        print('*** {} ***'.format(file_name))

        no_match_yet = True
        #
        # Process front page with info: cert_id, certified_item and developer
        #
        whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(file_name, NUM_LINES_TO_INVESTIGATE, LINE_SEPARATOR_STRICT)

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
                        print('string **{}** detected in certified item - shall not be here, fixing...'.format(from_keyword))
                        certified_item_first = certified_item[:certified_item.find(from_keyword)]
                        developer = certified_item[certified_item.find(from_keyword) + from_keyword_len:]
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
                items_found[TAG_CERT_ITEM] = normalize_match_string(certified_item)
                items_found[TAG_DEVELOPER] = normalize_match_string(developer)
                items_found[TAG_CERT_LAB] = 'BSI'

        #
        # Process page with more detailed certificate info
        # PP Conformance, Functionality, Assurance
        rules_certificate_third = [
            'PP Conformance: (.+)Functionality: (.+)Assurance: (.+)The IT Product identified',
        ]

        whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(file_name)

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

                items_found[TAG_REFERENCED_PROTECTION_PROFILES] = normalize_match_string(ref_protection_profiles)
                items_found[TAG_CC_VERSION] = normalize_match_string(cc_version)
                items_found[TAG_CC_SECURITY_LEVEL] = normalize_match_string(cc_security_level)

        if no_match_yet:
            files_without_match.append(file_name)

    if False:
        print_found_properties(items_found_all)

    with open("certificate_data_bsiheader.json", "w") as write_file:
        write_file.write(json.dumps(items_found_all, indent=4, sort_keys=True))

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    return items_found_all, files_without_match


def search_only_headers_anssi(walk_dir):
    class HEADER_TYPE(Enum):
        HEADER_FULL = 1
        HEADER_MISSING_CERT_ITEM_VERSION = 2
        HEADER_MISSING_PROTECTION_PROFILES = 3
        HEADER_DUPLICITIES = 4

    rules_certificate_preface = [
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.*)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.*)Conformité à un profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeurs(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)()Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur (.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom des produits(.+)Référence/version des produits(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur\(s\)(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom des produits(.+)Référence/version des produits(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur (.+)Centre d\'évaluation(.+)Accords de reconnaissance'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profils de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur\(s\)(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à un profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur\(s\)(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à un profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur (.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à des profils de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeurs(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profils de protection(.+)Critères d\’évaluation et version(.+)Niveau d\’évaluation(.+)Développeurs(.+)Centre d\’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit \(référence/version\)(.+)Nom de la TOE \(référence/version\)(.+)Conformité à un profil de protection(.+)Critères d\’évaluation et version(.+)Niveau d\’évaluation(.+)Développeurs(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profil de protection(.+)Critères d’évaluation et version(.+)Niveau d’évaluation(.+)Développeur\(s\)(.+)Centre d’évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur\(s\)(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit \(référence/version\)(.+)Nom de la TOE \(référence/version\)(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Conformité aux profils de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),

        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur\(s\)(.+)dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  des profils de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit \(rÃ©fÃ©rence/version\)(.+)Nom de la TOE \(rÃ©fÃ©rence/version\)(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Certification Report(.+)Nom du produit(.+)Référence/version du produit(.*)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© aux profisl de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centres dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)Version du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence/version du produit(.+)ConformitÃ© aux profils de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur\(s\)(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)Versions du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeur (.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'RÃ©fÃ©rence du rapport de certification(.+)Nom du produit(.+)RÃ©fÃ©rence du produit(.+)ConformitÃ© Ã  un profil de protection(.+)CritÃ¨res dâ€™Ã©valuation et version(.+)Niveau dâ€™Ã©valuation(.+)DÃ©veloppeurs(.+)Centre dâ€™Ã©valuation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_FULL, 'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer (.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL, 'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer (.+)Evaluation facility(.+)Mutual Recognition Agreements'),
        (HEADER_TYPE.HEADER_FULL, 'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developers(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL, 'Certification report reference(.+)Product name(.+)Product reference(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer\(s\)(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL, 'Certification report reference(.+)Products names(.+)Products references(.+)protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developers(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL, 'Certification report reference(.+)Product name \(reference / version\)(.+)TOE name \(reference / version\)(.+)Protection profile conformity(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developers(.+)Evaluation facility(.+)Recognition arrangements'),
        (HEADER_TYPE.HEADER_FULL, 'Certification report reference(.+)TOE name(.+)Product\'s reference/ version(.+)TOE\'s reference/ version(.+)Conformité à un profil de protection(.+)Evaluation criteria and version(.+)Evaluation level(.+)Developer (.+)Evaluation facility(.+)Recognition arrangements'),

        # corrupted text (duplicities)
        (HEADER_TYPE.HEADER_DUPLICITIES, 'RÃ©fÃ©rencce du rapport de d certification n(.+)Nom du p produit(.+)RÃ©fÃ©rencce/version du produit(.+)ConformiitÃ© Ã  un profil de d protection(.+)CritÃ¨res d dâ€™Ã©valuation ett version(.+)Niveau dâ€™â€™Ã©valuation(.+)DÃ©velopp peurs(.+)Centre dâ€™â€™Ã©valuation(.+)Accords d de reconnaisssance applicab bles'),

        # rules without product version
        (HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION, 'Référence du rapport de certification(.+)Nom et version du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION, 'Référence du rapport de certification(.+)Nom et version du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeur (.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
        (HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION, 'Référence du rapport de certification(.+)Nom du produit(.+)Conformité à un profil de protection(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),

        # rules without protection profile
        (HEADER_TYPE.HEADER_MISSING_PROTECTION_PROFILES, 'Référence du rapport de certification(.+)Nom du produit(.+)Référence/version du produit(.+)Critères d\'évaluation et version(.+)Niveau d\'évaluation(.+)Développeurs(.+)Centre d\'évaluation(.+)Accords de reconnaissance applicables'),
    ]

#    rules_certificate_preface = [
#        (HEADER_TYPE.HEADER_FULL, 'ddddd'),
#    ]

    # statistics about rules success rate
    num_rules_hits = {}
    for rule in rules_certificate_preface:
        num_rules_hits[rule[1]] = 0

    items_found_all = {}
    files_without_match = []
    for file_name in search_files(walk_dir):
        if not os.path.isfile(file_name):
            continue
        print('*** {} ***'.format(file_name))

        whole_text, whole_text_with_newlines, was_unicode_decode_error = load_cert_file(file_name)

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
                    print('WARNING: multiple rules are matching same certification document: ' + file_name)

                num_rules_hits[rule[1]] += 1  # add hit to this rule

                match_groups = m.groups()

                index_next_item = 0

                items_found[TAG_CERT_ID] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1

                items_found[TAG_CERT_ITEM] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1

                if rule[0] == HEADER_TYPE.HEADER_MISSING_CERT_ITEM_VERSION:
                    items_found[TAG_CERT_ITEM_VERSION] = ''
                else:
                    items_found[TAG_CERT_ITEM_VERSION] = normalize_match_string(match_groups[index_next_item])
                    index_next_item += 1

                if rule[0] == HEADER_TYPE.HEADER_MISSING_PROTECTION_PROFILES:
                    items_found[TAG_REFERENCED_PROTECTION_PROFILES] = ''
                else:
                    items_found[TAG_REFERENCED_PROTECTION_PROFILES] = normalize_match_string(match_groups[index_next_item])
                    index_next_item += 1

                items_found[TAG_CC_VERSION] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1

                items_found[TAG_CC_SECURITY_LEVEL] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1

                items_found[TAG_DEVELOPER] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1

                items_found[TAG_CERT_LAB] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1

                # we now have full raw header extracted - try to process raw data
                # items_found_all[file_name][TAG_CERT_HEADER_PROCESSED] = {}
                # items_found_all[file_name][TAG_CERT_HEADER_PROCESSED] = process_raw_header(items_found)

        if no_match_yet:
            files_without_match.append(file_name)

    if False:
        print_found_properties(items_found_all)

    # store results into file with fixed name and also with time appendix
    with open("certificate_data_anssiheader.json", "w") as write_file:
        write_file.write(json.dumps(items_found_all, indent=4, sort_keys=True))

    print('\n*** Certificates without detected preface:')
    for file_name in files_without_match:
        print('No hits for {}'.format(file_name))
    print('Total no hits files: {}'.format(len(files_without_match)))
    print('\n**********************************')

    if True:
        print('# hits for rule')
        sorted_rules = sorted(num_rules_hits.items(), key=operator.itemgetter(1), reverse=True)
        used_rules = []
        for rule in sorted_rules:
            print('{:4d} : {}'.format(rule[1], rule[0]))
            if rule[1] > 0:
                used_rules.append(rule[0])

    return items_found_all, files_without_match


def extract_certificates_frontpage(walk_dir):
    anssi_items_found, anssi_files_without_match = search_only_headers_anssi(walk_dir)
    bsi_items_found, bsi_files_without_match = search_only_headers_bsi(walk_dir)

    print('*** Files without detected header')
    files_without_match = list(set(anssi_files_without_match) & set(bsi_files_without_match))
    for file_name in files_without_match:
        print(file_name)
    print('Total no hits files: {}'.format(len(files_without_match)))

    items_found_all = {**anssi_items_found, **bsi_items_found}
    # store results into file with fixed name and also with time appendix
    with open("certificate_data_headers.json", "w") as write_file:
        write_file.write(json.dumps(items_found_all, indent=4, sort_keys=True))

    return items_found_all

def extract_certificates_keywords(walk_dir, fragments_dir):
    MIN_ITEMS_FOUND = 30655

    all_items_found = {}
    cert_id = {}
    for file_name in search_files(walk_dir):
        if not os.path.isfile(file_name):
            continue

        print('*** {} ***'.format(file_name))

        # parse certificate, return all matches
        all_items_found[file_name], modified_cert_file = parse_cert_file(file_name, rules, -1)

        # try to establish the certificate id of the current certificate
        cert_id[file_name] = estimate_cert_id(None, all_items_found[file_name], file_name)

        # save report text with highlighted/replaced matches into \\fragments\\ directory
        base_path = file_name[:file_name.rfind('\\')]
        file_name_short = file_name[file_name.rfind('\\') + 1:]
        target_file = '{}\\{}'.format(fragments_dir, file_name_short)
        save_modified_cert_file(target_file, modified_cert_file[0], modified_cert_file[1])

    # store results into file with fixed name and also with time appendix
    with open("certificate_data.json", "w") as write_file:
        write_file.write(json.dumps(all_items_found, indent=4, sort_keys=True))

    print('\nTotal matches found in separate files:')
    # print_total_matches_in_files(all_items_found_count)

    print('\nFile name and estimated certificate ID:')
    # print_guessed_cert_id(cert_id)

    #print_dot_graph_keywordsonly(['rules_cert_id'], all_items_found, cert_id, walk_dir, 'certid_graph_from_keywords.dot', True)

    total_items_found = 0
    for file_name in all_items_found:
        total_items_found += count_num_items_found(all_items_found[file_name])

    all_matches = []
    for file_name in all_items_found:
        for rule_group in all_items_found[file_name].keys():
            items_found = all_items_found[file_name][rule_group]
            for rule in items_found.keys():
                for match in items_found[rule]:
                    if match not in all_matches:
                        all_matches.append(match)

    sorted_all_matches = sorted(all_matches)
    for match in sorted_all_matches:
        print(match)

    # verify total matches found
    print('\nTotal matches found: {}'.format(total_items_found))
    if MIN_ITEMS_FOUND > total_items_found:
        print('ERROR: less items found!')
        if STOP_ON_INCORRECT_NUMS:
            print(error_less_matches_detected)

    return all_items_found

def parse_product_updates(updates_chunk, link_files_updates):
    maintenance_reports = []

    rule_with_maintainance_ST = '.*?([0-9]+?-[0-9]+?-[0-9]+?) (.+?)\<br style=' \
           '.*?\<a href="(.+?)" title="Maintenance Report' \
           '.*?\<a href="(.+?)" title="Maintenance ST'
    rule_without_maintainance_ST = '.*?([0-9]+?-[0-9]+?-[0-9]+?) (.+?)\<br style=' \
           '.*?\<a href="(.+?)" title="Maintenance Report'\

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
                items_found['maintenance_date'] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1
                items_found['maintenance_item_name'] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1
                items_found['maintenance_link_cert_report'] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1
                if len(match_groups) > index_next_item:
                    items_found['maintenance_link_security_target'] = normalize_match_string(match_groups[index_next_item])
                    index_next_item += 1
                else:
                    items_found['maintenance_link_security_target'] = ""

                link_files_updates.append((items_found['maintenance_link_cert_report'], items_found['maintenance_link_security_target']))

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
        rule = '\<br\>(.+?) '       # items are in form of <br>AVA_VLA.4 <br>AVA_MSU.3 ...

        for m in re.finditer(rule, augm_chunk):
            match_groups = m.groups()
            eal_augmented.append(normalize_match_string(match_groups[0]))

    return eal_level, eal_augmented


def extract_certificates_metadata_html(file_name):
    items_found_all = {}
    download_files_certs = []
    download_files_updates = []
    print('*** {} ***'.format(file_name))

    whole_text = load_cert_html_file(file_name)

    whole_text = whole_text.replace('\n', ' ')
    whole_text = whole_text.replace('&nbsp;', ' ')
    whole_text = whole_text.replace('&amp;', '&')

    # First find end extract chunks between <tr class=""> ... </tr>
    start_pos = whole_text.find('<tfoot class="hilite7"><!-- hilite1 -->')
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
            HEADER_MISSING_VENDOR_WEB = 2

        # IMPORTANT: order regexes based on their specificity - the most specific goes first
        rules_cc_html = [
            (HEADER_TYPE.HEADER_FULL, '\<tr class=(?:""|"even")\>[ ]+\<td class="b"\>(.+?)\<a name="(.+?)" style=.+?\<!-- \<a href="(.+?)" title="Vendor\'s web site" target="_blank"\>(.+?)</a> -->'
            '.+?\<a href="(.+?)" title="Certification Report:.+?" target="_blank" class="button2"\>Certification Report\</a\>'
            '.+?\<a href="(.+?)" title="Security Target:.+?" target="_blank" class="button2">Security Target</a>'
            '.+?\<!-- ------ ------ ------ Product Updates ------ ------ ------ --\>'
            '(.+?)<!-- ------ ------ ------ END Product Updates ------ ------ ------ --\>'
            '.+?\<!--end-product-cell--\>'
            '.+?\<td style="text-align:center"\>\<span title=".+?"\>(.+?)\</span\>\</td\>'
            '.+?\<td style="text-align:center"\>(.*?)\</td\>'
            '[ ]+?\<td>(.+?)\</td\>'),

            (HEADER_TYPE.HEADER_MISSING_VENDOR_WEB,'\<tr class=(?:""|"even")\>[ ]+\<td class="b"\>(.+?)\<a name="(.+?)" style=.+?'
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
                continue    # search only the first match


            rule_and_sep = rule[1]

            for m in re.finditer(rule_and_sep, chunk):
                if no_match_yet:
                    chunks_matched += 1
                    items_found = {}
                    #items_found_all.append(items_found)
                    items_found[TAG_HEADER_MATCH_RULES] = []
                    no_match_yet = False

                # insert rule if at least one match for it was found
                #if rule not in items_found[TAG_HEADER_MATCH_RULES]:
                    # items_found[TAG_HEADER_MATCH_RULES].append(rule[1])

                match_groups = m.groups()

                index_next_item = 0
                items_found['cert_item_name'] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1
                items_found['cc_cert_item_html_id'] = normalize_match_string(match_groups[index_next_item])
                cert_item_id = items_found['cc_cert_item_html_id']
                index_next_item += 1
                if not rule[0] == HEADER_TYPE.HEADER_MISSING_VENDOR_WEB:
                    items_found['company_site'] = normalize_match_string(match_groups[index_next_item])
                    index_next_item += 1
                    items_found['company_name'] = normalize_match_string(match_groups[index_next_item])
                    index_next_item += 1
                items_found['link_cert_report'] = normalize_match_string(match_groups[index_next_item])
                link_cert_report = items_found['link_cert_report']
                items_found['link_cert_report_file_name'] = link_cert_report[link_cert_report.rfind('/') + 1:]
                cert_file_name = items_found['link_cert_report_file_name']
                index_next_item += 1
                items_found['link_security_target'] = normalize_match_string(match_groups[index_next_item])
                download_files_certs.append((items_found['link_cert_report'], items_found['link_security_target']))
                index_next_item += 1

                items_found['product_updates'] = parse_product_updates(match_groups[index_next_item], download_files_updates)
                index_next_item += 1

                items_found['date_cert_issued'] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1
                items_found['date_cert_expiration'] = normalize_match_string(match_groups[index_next_item])
                index_next_item += 1
                cc_security_level = normalize_match_string(match_groups[index_next_item])
                items_found['cc_security_level'], items_found['cc_security_level_augmented'] = parse_security_level(cc_security_level)
                index_next_item += 1


                # prepare unique name for dictionary (file name is not enough as multiple records reference same cert)
                cert_file_name = cert_file_name.replace('%20', '0')
                item_unique_name = '{}__{}'.format(cert_file_name, cert_item_id)
                if item_unique_name not in items_found_all.keys():
                    items_found_all[item_unique_name] = {}
                    items_found_all[item_unique_name]['html_scan'] = items_found
                else:
                    print('{} already in'.format(cert_file_name))

                continue  # we are interested only in first match

        if no_match_yet:
            print('No match found in block #{}'.format(chunks_found))

    print('Chunks found: {}, Chunks matched: {}'.format(chunks_found, chunks_matched))
    if chunks_found != chunks_matched:
        print('WARNING: not all chunks found were matched')

    return items_found_all, download_files_certs, download_files_updates


def generate_download_script(file_name, certs_dir, targets_dir, download_files_certs):
    with open(file_name, "w") as write_file:
        # certs files
        write_file.write('mkdir \"{}\"\n'.format(certs_dir))
        write_file.write('cd \"{}\"\n\n'.format(certs_dir))
        for cert in download_files_certs:
            write_file.write('curl \"{}\" --remote-name\n'.format(cert[0]))
            write_file.write('pdftotext \"{}\"\n\n'.format(cert[0][cert[0].rfind('/') + 1 : ]))

        # security targets file
        write_file.write('\n\ncd ..\n')
        write_file.write('mkdir \"{}\"\n'.format(targets_dir))
        write_file.write('cd \"{}\"\n\n'.format(targets_dir))
        for cert in download_files_certs:
            write_file.write('curl \"{}\" --remote-name\n'.format(cert[1]))
            write_file.write('pdftotext \"{}\"\n\n'.format(cert[1][cert[1].rfind('/') + 1 : ]))


def extract_certificates_html(base_dir):
    file_name = '{}common_criteria_products_active.html'.format(base_dir)
    items_found_all_active, download_files_certs, download_files_updates = extract_certificates_metadata_html(file_name)
    for item in items_found_all_active.keys():
        items_found_all_active[item]['html_scan']['cert_status'] = 'active'

    with open("certificate_data_html_active.json", "w") as write_file:
        write_file.write(json.dumps(items_found_all_active, indent=4, sort_keys=True))

    generate_download_script('download_active_certs.bat', 'certs', 'targets', download_files_certs)
    generate_download_script('download_active_updates.bat', 'certs', 'targets', download_files_updates)

    file_name = '{}common_criteria_products_archived.html'.format(base_dir)
    items_found_all_archived, download_files_certs, download_files_updates = extract_certificates_metadata_html(file_name)
    for item in items_found_all_archived.keys():
        items_found_all_archived[item]['html_scan']['cert_status'] = 'archived'

    with open("certificate_data_html_archived.json", "w") as write_file:
        write_file.write(json.dumps(items_found_all_archived, indent=4, sort_keys=True))

    generate_download_script('download_archived_certs.bat', 'certs', 'targets', download_files_certs)
    generate_download_script('download_archived_updates.bat', 'certs', 'targets', download_files_updates)

    items_found_all = {**items_found_all_active, **items_found_all_archived}
    with open("certificate_data_html_all.json", "w") as write_file:
        write_file.write(json.dumps(items_found_all, indent=4, sort_keys=True))

    return items_found_all


def collate_certificates_data(all_html, all_front, all_keywords):
    print('\n\nPairing results from different scans ***')
    all_cert_items = all_html
    # pair html data, front pages and keywords
    for file_name in all_keywords.keys():
        pairing_found = False

        file_name_pdf = file_name[file_name.rfind('\\') + 1:]
        file_name_pdf = file_name_pdf[:file_name_pdf.rfind('.')] + '.pdf'

        # find all items which references same pdf report
        for file_and_id in all_html.keys():
            # in items extracted from html, names are in form of 'file_name.pdf__number'
            if file_and_id.find(file_name_pdf + '__') != -1:
                if 'processed' not in all_cert_items[file_and_id].keys():
                    all_cert_items[file_and_id]['processed'] = {}
                pairing_found = True
                frontpage_scan = None
                keywords_scan = None
                if file_name in all_front.keys():
                    all_cert_items[file_and_id]['frontpage_scan'] = all_front[file_name]
                    frontpage_scan = all_front[file_name]
                if file_name in all_keywords.keys():
                    all_cert_items[file_and_id]['keywords_scan'] = all_keywords[file_name]
                    keywords_scan = all_keywords[file_name]

                all_cert_items[file_and_id]['processed']['cert_id'] = estimate_cert_id(frontpage_scan, keywords_scan, file_name)

        if not pairing_found:
            print('Corresponding report not found for {}'.format(file_and_id))

    with open("certificate_data_complete.json", "w") as write_file:
        write_file.write(json.dumps(all_cert_items, indent=4, sort_keys=True))

    # display all record which were not paired
    print('Records with missing pairing of frontpage and keywords:')
    num_frontpage_missing = 0
    num_keywords_missing = 0
    for item in all_cert_items.keys():
        this_item = all_cert_items[item]
        if 'frontpage_scan' not in this_item.keys():
            print('{}: no frontpage scan detected'.format(item))
            num_frontpage_missing += 1
        if 'keywords_scan' not in this_item.keys():
            print('{}: no keywords scan detected'.format(item))
            num_keywords_missing += 1

    print('Records without frontpage: {}\nRecords without keywords: {}'.format(num_frontpage_missing, num_keywords_missing))

    return all_cert_items


def generate_dot_graphs(all_items_found, walk_dir):
    print_dot_graph(['rules_cert_id'], all_items_found, walk_dir, 'certid_graph.dot', True)
    print_dot_graph(['rules_javacard'], all_items_found, walk_dir, 'cert_javacard_graph.dot', False)

    #    print_dot_graph(['rules_security_level'], all_items_found, walk_dir, 'cert_security_level_graph.dot', True)
    #    print_dot_graph(['rules_crypto_libs'], all_items_found, walk_dir, 'cert_crypto_libs_graph.dot', False)
    #    print_dot_graph(['rules_vendor'], all_items_found, walk_dir, 'rules_vendor.dot', False)
    #    print_dot_graph(['rules_crypto_algs'], all_items_found, walk_dir, 'rules_crypto_algs.dot', False)
    #    print_dot_graph(['rules_protection_profiles'], all_items_found, walk_dir, 'rules_protection_profiles.dot', False)
    #    print_dot_graph(['rules_defenses'], all_items_found, walk_dir, 'rules_defenses.dot', False)


def main():
    # change current directory to store results into results file
    current_dir = os.getcwd()
    os.chdir(current_dir + '\\..\\results\\')

    walk_dir = 'c:\\Certs\\certs\\cc_search\\20191109_icsconly_currentandachived\\'
    #walk_dir = 'c:\\Certs\\certs\\cc_search\\20191109_icsconly_currentandachived_bsionly\\'
    #walk_dir = 'c:\\Certs\\certs\\cc_search\\20191109_icsconly_currentandachived_anssionly\\'
    #walk_dir = 'c:\\Certs\\certs\\cc_search\\test6\\'
    cc_html_files_dir = 'c:\\Certs\\certs\\cc_search\\'
    walk_dir = 'c:\\Certs\\cc_certs_txt\\'
    #walk_dir = 'c:\\Certs\\cc_certs_txt_test2\\'
    fragments_dir = 'c:\\Certs\\cc_certs_txt_fragments\\'

    do_extraction = False

    if do_extraction:
        all_html = extract_certificates_html(cc_html_files_dir)

        all_front = extract_certificates_frontpage(walk_dir)

        all_keywords = extract_certificates_keywords(walk_dir, fragments_dir)

        all_cert_items = collate_certificates_data(all_html, all_front, all_keywords)

    with open('certificate_data_complete.json') as json_file:
        all_cert_items = json.load(json_file)

    analyze_references_graph(['rules_cert_id'], all_cert_items)

    generate_dot_graphs(all_cert_items, walk_dir)


if __name__ == "__main__":
    main()