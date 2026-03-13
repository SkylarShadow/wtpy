from ztpy.apps import ZtBtAnalyst

if __name__ == "__main__":

    analyst = ZtBtAnalyst()
    analyst.add_strategy("Dt_IF_SP_spTicks_155", folder="./outputs_bt/Dt_IF_SP_spTicks_155/", init_capital=1200000, rf=0.02, annual_trading_days=240)
    analyst.run()

    kw = input('press any key to exit\n')