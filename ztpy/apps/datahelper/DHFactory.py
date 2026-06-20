from ztpy.apps.datahelper.DHDefs import BaseDataHelper

class DHFactory:
    
    @staticmethod
    def createHelper(name:str) -> BaseDataHelper:
        '''
        创建数据辅助模块\n
        @name   模块名称，目前支持的有tushare、baostock、rqdata
        '''
        name = name.lower()
        if name == "baostock":
            from ztpy.apps.datahelper.DHBaostock import DHBaostock
            return DHBaostock()
        elif name == "tushare":
            from ztpy.apps.datahelper.DHTushare import DHTushare
            return DHTushare()
        elif name == "rqdata":
            from ztpy.apps.datahelper.DHRqData import DHRqData
            return DHRqData()
        elif name == "tqsdk":
            from ztpy.apps.datahelper.DHTqSdk import DHTqSdk
            return DHTqSdk()
        elif name == "tdx":
            from ztpy.apps.datahelper.DHTdx import DHTdx
            return DHTdx()
        else:
            raise Exception("Cannot recognize helper with name %s" % (name))
