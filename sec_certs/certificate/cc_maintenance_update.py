import copy
import logging
from abc import ABC
from typing import Optional, Dict, List, ClassVar
from datetime import date

import sec_certs.helpers as helpers
from sec_certs.certificate.common_criteria import CommonCriteriaCert
from sec_certs.certificate.certificate import Certificate
from sec_certs.serialization import ComplexSerializableType


logger = logging.getLogger(__name__)


class CommonCriteriaMaintenanceUpdate(CommonCriteriaCert, ComplexSerializableType):
    pandas_columns: ClassVar[List[str]] = ['dgst', 'name', 'report_link', 'st_link', 'extracted_versions',
                                           'related_cert_digest', 'maintenance_date']

    def __init__(self, name: str, report_link: str, st_link: str,
                 state: Optional[CommonCriteriaCert.InternalState],
                 pdf_data: Optional[CommonCriteriaCert.PdfData],
                 heuristics: Optional[CommonCriteriaCert.Heuristics],
                 related_cert_digest: str,
                 maintenance_date: date):
        super().__init__('', '', name, '', '', '', None, None,
                         report_link, st_link, '', '', set(), set(),
                         state, pdf_data, heuristics)
        self.related_cert_digest = related_cert_digest
        self.maintenance_date = maintenance_date

    @property
    def serialized_attributes(self) -> List[str]:
        return ['name', 'report_link', 'st_link', 'state', 'pdf_data', 'heuristics', 'related_cert_digest', 'maintenance_date']

    @property
    def dgst(self):
        return 'cert_' + self.related_cert_digest + '_update_' + helpers.get_first_16_bytes_sha256(self.name)

    @classmethod
    def from_dict(cls, dct: Dict) -> 'CommonCriteriaMaintenanceUpdate':
        return Certificate.from_dict(dct)

    @classmethod
    def get_updates_from_cc_cert(cls, cert: CommonCriteriaCert):
        return [cls(x.maintainance_title, x.maintainance_report_link, x.maintainance_st_link,
                    None, None, None, cert.dgst, x.maintainance_date) for x in cert.maintainance_updates]