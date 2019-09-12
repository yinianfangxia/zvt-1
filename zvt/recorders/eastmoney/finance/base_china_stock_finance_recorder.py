# -*- coding: utf-8 -*-
from jqdatasdk import auth, query, indicator, get_fundamentals, logout

from zvdata.api import get_data
from zvdata.utils.pd_utils import df_is_not_null
from zvt.api.api import get_finance_factor
from zvt.api.common import to_jq_report_period
from zvt.domain import FinanceFactor
from zvt.recorders.eastmoney.common import company_type_flag, get_fc, EastmoneyTimestampsDataRecorder, \
    call_eastmoney_api, get_from_path_fields
from zvt.recorders.joinquant import to_jq_entity_id
from zvt.settings import JQ_ACCOUNT, JQ_PASSWD
from zvdata.utils.pd_utils import index_df
from zvdata.utils.time_utils import to_time_str, to_pd_timestamp


class BaseChinaStockFinanceRecorder(EastmoneyTimestampsDataRecorder):
    finance_report_type = None
    data_type = 1

    timestamps_fetching_url = 'https://emh5.eastmoney.com/api/CaiWuFenXi/GetCompanyReportDateList'
    timestamp_list_path_fields = ['CompanyReportDateList']
    timestamp_path_fields = ['ReportDate']

    def __init__(self, entity_type='stock', exchanges=['sh', 'sz'], entity_ids=None, codes=None, batch_size=10,
                 force_update=False, sleeping_time=5, default_size=2000, one_shot=False,
                 fix_duplicate_way='add') -> None:
        super().__init__(entity_type, exchanges, entity_ids, codes, batch_size, force_update, sleeping_time,
                         default_size, one_shot, fix_duplicate_way)

        auth(JQ_ACCOUNT, JQ_PASSWD)

    def init_timestamps(self, entity):
        param = {
            "color": "w",
            "fc": get_fc(entity),
            "DataType": self.data_type
        }

        if self.finance_report_type == 'LiRunBiaoList' or self.finance_report_type == 'XianJinLiuLiangBiaoList':
            param['ReportType'] = 1

        timestamp_json_list = call_eastmoney_api(url=self.timestamps_fetching_url,
                                                 path_fields=self.timestamp_list_path_fields,
                                                 param=param)

        if self.timestamp_path_fields:
            timestamps = [get_from_path_fields(data, self.timestamp_path_fields) for data in timestamp_json_list]

        return [to_pd_timestamp(t) for t in timestamps]

    def generate_request_param(self, security_item, start, end, size, timestamps):
        if len(timestamps) <= 10:
            param = {
                "color": "w",
                "fc": get_fc(security_item),
                "corpType": company_type_flag(security_item),
                # 0 means get all types
                "reportDateType": 0,
                "endDate": '',
                "latestCount": size
            }
        else:
            param = {
                "color": "w",
                "fc": get_fc(security_item),
                "corpType": company_type_flag(security_item),
                # 0 means get all types
                "reportDateType": 0,
                "endDate": to_time_str(timestamps[10]),
                "latestCount": 10
            }

        if self.finance_report_type == 'LiRunBiaoList' or self.finance_report_type == 'XianJinLiuLiangBiaoList':
            param['reportType'] = 1

        return param

    def generate_path_fields(self, security_item):
        comp_type = company_type_flag(security_item)

        if comp_type == "3":
            return ['{}_YinHang'.format(self.finance_report_type)]
        elif comp_type == "2":
            return ['{}_BaoXian'.format(self.finance_report_type)]
        elif comp_type == "1":
            return ['{}_QuanShang'.format(self.finance_report_type)]
        elif comp_type == "4":
            return ['{}_QiYe'.format(self.finance_report_type)]

    def record(self, entity, start, end, size, timestamps):
        # different with the default timestamps handling
        param = self.generate_request_param(entity, start, end, size, timestamps)
        self.logger.info('request param:{}'.format(param))

        return self.api_wrapper.request(url=self.url, param=param, method=self.request_method,
                                        path_fields=self.generate_path_fields(entity))

    def get_original_time_field(self):
        return 'ReportDate'

    def fill_timestamp_with_jq(self, security_item, the_data):
        # get report published date from jq
        q = query(
            indicator.pubDate
        ).filter(
            indicator.code == to_jq_entity_id(security_item),
        )

        df = get_fundamentals(q, statDate=to_jq_report_period(the_data.report_date))
        if not df.empty:
            the_data.timestamp = to_pd_timestamp(df['pubDate'][0])
            self.logger.info(
                'jq fill {} {} timestamp:{} for report_date:{}'.format(self.data_schema, security_item.id,
                                                                       the_data.timestamp,
                                                                       the_data.report_date))
            self.session.commit()

    def on_finish_entity(self, entity):
        # fill the timestamp for report published date
        the_data_list = get_data(data_schema=self.data_schema,
                                 provider=self.provider,
                                 entity_id=entity.id,
                                 order=self.data_schema.timestamp.asc(),
                                 return_type='domain',
                                 session=self.session,
                                 filters=[self.data_schema.timestamp == self.data_schema.report_date,
                                          self.data_schema.timestamp >= to_pd_timestamp('2005-01-01')])
        if the_data_list:
            if self.data_schema == FinanceFactor:
                for the_data in the_data_list:
                    self.fill_timestamp_with_jq(entity, the_data)
            else:
                df = get_finance_factor(entity_id=entity.id,
                                         columns=[FinanceFactor.timestamp, FinanceFactor.report_date, FinanceFactor.id],
                                         filters=[FinanceFactor.timestamp != FinanceFactor.report_date,
                                                  FinanceFactor.timestamp >= to_pd_timestamp('2005-01-01'),
                                                  FinanceFactor.report_date >= the_data_list[0].report_date,
                                                  FinanceFactor.report_date <= the_data_list[-1].report_date, ])

                if df_is_not_null(df):
                    index_df(df, index='report_date')

                for the_data in the_data_list:
                    if (df is not None) and (not df.empty) and the_data.report_date in df.index:
                        the_data.timestamp = df.at[the_data.report_date, 'timestamp']
                        self.logger.info(
                            'db fill {} {} timestamp:{} for report_date:{}'.format(self.data_schema, entity.id,
                                                                                   the_data.timestamp,
                                                                                   the_data.report_date))
                        self.session.commit()
                    else:
                        # self.logger.info(
                        #     'waiting jq fill {} {} timestamp:{} for report_date:{}'.format(self.data_schema,
                        #                                                                    security_item.id,
                        #                                                                    the_data.timestamp,
                        #                                                                    the_data.report_date))

                        self.fill_timestamp_with_jq(entity, the_data)

    def on_finish(self):
        super().on_finish()
        logout()
