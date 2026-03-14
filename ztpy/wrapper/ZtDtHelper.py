from ctypes import cdll, CFUNCTYPE, c_char_p, c_void_p, c_bool, POINTER, c_uint32, c_uint64
from ztpy.ZtCoreDefs import ZTSTickStruct, ZTSBarStruct, ZTSOrdDtlStruct, ZTSOrdQueStruct, ZTSTransStruct
from ztpy.ZtDataDefs import ZtTickCache, ZtNpOrdDetails, ZtNpOrdQueues, ZtNpTransactions, ZtNpKline, ZtNpTicks, ZtBarCache
from ztpy.SessionMgr import SessionInfo
from ztpy.wrapper.PlatformHelper import PlatformHelper as ph
from ztpy.ZtUtilDefs import singleton
import os,logging

CB_DTHELPER_LOG = CFUNCTYPE(c_void_p,  c_char_p)
CB_DTHELPER_TICK = CFUNCTYPE(c_void_p,  POINTER(ZTSTickStruct), c_uint32, c_bool)
CB_DTHELPER_ORDQUE = CFUNCTYPE(c_void_p,  POINTER(ZTSOrdDtlStruct), c_uint32, c_bool)
CB_DTHELPER_ORDDTL = CFUNCTYPE(c_void_p,  POINTER(ZTSOrdQueStruct), c_uint32, c_bool)
CB_DTHELPER_TRANS = CFUNCTYPE(c_void_p,  POINTER(ZTSTransStruct), c_uint32, c_bool)
CB_DTHELPER_BAR = CFUNCTYPE(c_void_p,  POINTER(ZTSBarStruct), c_uint32, c_bool)

CB_DTHELPER_COUNT = CFUNCTYPE(c_void_p,  c_uint32)

@singleton
class ZtDataHelper:
    '''
    平台数据组件C接口底层对接模块
    '''

    # api可以作为公共变量
    api = None
    ver = "Unknown"

    # 构造函数，传入动态库名
    def __init__(self):
        paths = os.path.split(__file__)
        dllname = ph.getModule("ZtDtHelper")
        a = (paths[:-1] + (dllname,))
        _path = os.path.join(*a)
        self.api = cdll.LoadLibrary(_path)
        
        self.cb_dthelper_log = CB_DTHELPER_LOG(self.on_log_output)
        self.api.resample_bars.argtypes = [c_char_p, CB_DTHELPER_BAR, CB_DTHELPER_COUNT, c_uint64, c_uint64, c_char_p, c_uint32, c_char_p, CB_DTHELPER_LOG]

    def on_log_output(self, message:str):
        message = bytes.decode(message, 'utf-8')
        logging.info(message)

    def dump_bars(self, binFolder:str, csvFolder:str, strFilter:str=""):
        '''
        将目录下的.dsb格式的历史K线数据导出为.csv格式
        @binFolder  .dsb文件存储目录
        @csvFolder  .csv文件的输出目录
        @strFilter  代码过滤器(暂未启用)
        '''
        self.api.dump_bars(bytes(binFolder, encoding="utf8"), bytes(csvFolder, encoding="utf8"), bytes(strFilter, encoding="utf8"), self.cb_dthelper_log)

    def dump_ticks(self, binFolder: str, csvFolder: str, strFilter: str=""):
        '''
        将目录下的.dsb格式的历史Tik数据导出为.csv格式
        @binFolder  .dsb文件存储目录
        @csvFolder  .csv文件的输出目录
        @strFilter  代码过滤器(暂未启用)
        '''
        self.api.dump_ticks(bytes(binFolder, encoding="utf8"), bytes(csvFolder, encoding="utf8"), bytes(strFilter, encoding="utf8"), self.cb_dthelper_log)

    def trans_csv_bars(self, csvFolder: str, binFolder: str, period: str):
        '''
        将目录下的.csv格式的历史K线数据转成.dsb格式
        @csvFolder  .csv文件的输出目录
        @binFolder  .dsb文件存储目录
        @period     K线周期，m1-1分钟线，m5-5分钟线，d-日线
        '''
        self.api.trans_csv_bars(bytes(csvFolder, encoding="utf8"), bytes(binFolder, encoding="utf8"), bytes(period, encoding="utf8"), self.cb_dthelper_log)
    
    def trans_bars(self, barFile:str, getter, count:int, period:str) -> bool:
        '''
        将K线转储到dsb文件中
        @barFile    要存储的文件路径
        @getter     获取bar的回调函数
        @count      一共要写入的数据条数
        @period     周期，m1/m5/d
        '''
        raise Exception("Method trans_bars is removed from core, use store_bars instead")
        # cb = CB_DTHELPER_BAR_GETTER(getter)
        # return self.api.trans_bars(bytes(barFile, encoding="utf8"), cb, count, bytes(period, encoding="utf8"), self.cb_dthelper_log)

    def trans_ticks(self, tickFile:str, getter, count:int) -> bool:
        '''
        将Tick数据转储到dsb文件中
        @tickFile   要存储的文件路径
        @getter     获取tick的回调函数
        @count      一共要写入的数据条数
        '''
        raise Exception("Method trans_ticks is removed from core, use store_ticks instead")
        # cb = CB_DTHELPER_TICK_GETTER(getter)
        # return self.api.trans_ticks(bytes(tickFile, encoding="utf8"), cb, count, self.cb_dthelper_log)

    def store_bars(self, barFile:str, firstBar:POINTER(ZTSBarStruct), count:int, period:str) -> bool:
        '''
        将K线转储到dsb文件中
        @barFile    要存储的文件路径
        @firstBar   第一条bar的指针
        @count      一共要写入的数据条数
        @period     周期，m1/m5/d
        '''
        return self.api.store_bars(bytes(barFile, encoding="utf8"), firstBar, count, bytes(period, encoding="utf8"), self.cb_dthelper_log)

    def store_ticks(self, tickFile:str, firstTick:POINTER(ZTSTickStruct), count:int) -> bool:
        '''
        将Tick数据转储到dsb文件中
        @tickFile   要存储的文件路径
        @firstTick  第一条tick的指针
        @count      一共要写入的数据条数
        '''
        # cb = CB_DTHELPER_TICK_GETTER(getter)
        return self.api.store_ticks(bytes(tickFile, encoding="utf8"), firstTick, count, self.cb_dthelper_log)
    
    def store_order_details(self, targetFile:str, firstItem:POINTER(ZTSOrdDtlStruct), count:int) -> bool:
        '''
        将委托明细数据转储到dsb文件中
        @tickFile   要存储的文件路径
        @firstItem  第一条数据的指针
        @count      一共要写入的数据条数
        '''
        return self.api.store_order_details(bytes(targetFile, encoding="utf8"), firstItem, count, self.cb_dthelper_log)
    
    def store_order_queues(self, targetFile:str, firstItem:POINTER(ZTSOrdQueStruct), count:int) -> bool:
        '''
        将委托队列数据转储到dsb文件中
        @tickFile   要存储的文件路径
        @firstItem  第一条数据的指针
        @count      一共要写入的数据条数
        '''
        return self.api.store_order_queues(bytes(targetFile, encoding="utf8"), firstItem, count, self.cb_dthelper_log)
    
    def store_transactions(self, targetFile:str, firstItem:POINTER(ZTSTransStruct), count:int) -> bool:
        '''
        将逐笔成交数据转储到dsb文件中
        @tickFile   要存储的文件路径
        @firstItem  第一条数据的指针
        @count      一共要写入的数据条数
        '''
        return self.api.store_transactions(bytes(targetFile, encoding="utf8"), firstItem, count, self.cb_dthelper_log)
    
    def read_dsb_bars(self, barFile: str) -> ZtNpKline:
        '''
        读取.dsb格式的K线数据
        @tickFile   .dsb的K线数据文件
        @return     ZtNpKline,可以通过ZtNpKline.ndarray获取numpy的ndarray对象
        '''        
        bar_cache = ZtBarCache(forceCopy=True)
        if 0 == self.api.read_dsb_bars(bytes(barFile, encoding="utf8"), CB_DTHELPER_BAR(bar_cache.on_read_bar), CB_DTHELPER_COUNT(bar_cache.on_data_count), self.cb_dthelper_log):
            return None
        else:
            return bar_cache.records

    def read_dmb_ticks(self, tickFile: str) -> ZtNpTicks:
        '''
        读取.dmb格式的tick数据
        @tickFile   .dmb的tick数据文件
        @return     ZtNpTicks
        '''        
        tick_cache = ZtTickCache(forceCopy=True)
        if 0 == self.api.read_dmb_ticks(bytes(tickFile, encoding="utf8"), CB_DTHELPER_TICK(tick_cache.on_read_tick), CB_DTHELPER_COUNT(tick_cache.on_data_count), self.cb_dthelper_log):
            return None
        else:
            return tick_cache.records

    def read_dmb_bars(self, barFile: str) -> ZtNpKline:
        '''
        读取.dmb格式的K线数据
        @tickFile   .dmb的K线数据文件
        @return     ZtNpKline
        '''        
        bar_cache = ZtBarCache(forceCopy=True)
        if 0 == self.api.read_dmb_bars(bytes(barFile, encoding="utf8"), CB_DTHELPER_BAR(bar_cache.on_read_bar), CB_DTHELPER_COUNT(bar_cache.on_data_count), self.cb_dthelper_log):
            return None
        else:
            return bar_cache.records

    def read_dsb_ticks(self, tickFile: str) -> ZtNpTicks:
        '''
        读取.dsb格式的tick数据
        @tickFile   .dsb的tick数据文件
        @return     ZtNpTicks,可以通过ZtNpTicks.ndarray获取numpy的ndarray对象
        '''         
        tick_cache = ZtTickCache(forceCopy=True)
        if 0 == self.api.read_dsb_ticks(bytes(tickFile, encoding="utf8"), CB_DTHELPER_TICK(tick_cache.on_read_tick), CB_DTHELPER_COUNT(tick_cache.on_data_count), self.cb_dthelper_log):
            return None
        else:
            return tick_cache.records

    def read_dsb_order_details(self, dataFile: str) -> ZtNpOrdDetails:
        '''
        读取.dsb格式的委托明细数据
        @dataFile   .dsb的数据文件
        @return     ZtNpOrdDetails
        '''
        class DataCache:
            def __init__(self):
                self.records:ZtNpOrdDetails = None

            def on_read_data(self, firstItem:POINTER(ZTSOrdDtlStruct), count:int, isLast:bool):
                self.records = ZtNpOrdDetails(forceCopy=True)
                self.records.set_data(firstItem, count)

            def on_data_count(self, count:int):
                pass
        
        data_cache = DataCache()
        if 0 == self.api.read_dsb_order_details(bytes(dataFile, encoding="utf8"), CB_DTHELPER_ORDDTL(data_cache.on_read_data), CB_DTHELPER_COUNT(data_cache.on_data_count), self.cb_dthelper_log):
            return None
        else:
            return data_cache.records
        
    def read_dsb_order_queues(self, dataFile: str) -> ZtNpOrdQueues:
        '''
        读取.dsb格式的委托队列数据
        @dataFile   .dsb的数据文件
        @return     ZtNpOrdQueues
        '''
        class DataCache:
            def __init__(self):
                self.records:ZtNpOrdQueues = None

            def on_read_data(self, firstItem:POINTER(ZTSOrdQueStruct), count:int, isLast:bool):
                self.records = ZtNpOrdQueues(forceCopy=True)
                self.records.set_data(firstItem, count)

            def on_data_count(self, count:int):
                pass
        
        data_cache = DataCache()
        if 0 == self.api.read_dsb_order_queues(bytes(dataFile, encoding="utf8"), CB_DTHELPER_ORDQUE(data_cache.on_read_data), CB_DTHELPER_COUNT(data_cache.on_data_count), self.cb_dthelper_log):
            return None
        else:
            return data_cache.records
        
    def read_dsb_transactions(self, dataFile: str) -> ZtNpTransactions:
        '''
        读取.dsb格式的逐笔成交数据
        @dataFile   .dsb的数据文件
        @return     ZtNpTransactions
        '''
        class DataCache:
            def __init__(self):
                self.records:ZtNpTransactions = None

            def on_read_data(self, firstItem:POINTER(ZTSTransStruct), count:int, isLast:bool):
                self.records = ZtNpTransactions(forceCopy=True)
                self.records.set_data(firstItem, count)

            def on_data_count(self, count:int):
                pass
        
        data_cache = DataCache()
        if 0 == self.api.read_dsb_transactions(bytes(dataFile, encoding="utf8"), CB_DTHELPER_TRANS(data_cache.on_read_data), CB_DTHELPER_COUNT(data_cache.on_data_count), self.cb_dthelper_log):
            return None
        else:
            return data_cache.records
    
    def resample_bars(self, barFile:str, period:str, times:int, fromTime:int, endTime:int, sessInfo:SessionInfo, alignSection:bool = False) -> ZtNpKline:
        '''
        重采样K线
        @barFile    dsb格式的K线数据文件
        @period     基础K线周期，m1/m5/d
        @times      重采样倍数，如利用m1生成m3数据时，times为3
        @fromTime   开始时间，日线数据格式yyyymmdd，分钟线数据为格式为yyyymmddHHMMSS
        @endTime    结束时间，日线数据格式yyyymmdd，分钟线数据为格式为yyyymmddHHMMSS
        @sessInfo   交易时间模板
        '''        
        bar_cache = ZtBarCache(forceCopy=True)
        if 0 == self.api.resample_bars(bytes(barFile, encoding="utf8"), CB_DTHELPER_BAR(bar_cache.on_read_bar), CB_DTHELPER_COUNT(bar_cache.on_data_count), 
                fromTime, endTime, bytes(period,'utf8'), times, bytes(sessInfo.toString(),'utf8'), self.cb_dthelper_log, alignSection):
            return None
        else:
            return bar_cache.records