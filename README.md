# 极简量化交易设计

这套代码故意不接 CTP、不接数据库、不接 Web，只保留量化交易里最核心的四层：

1. `Bar`
   一根 K 线数据。
2. `Strategy`
   看到历史 K 线后，决定买、卖或不动。
3. `Broker`
   负责账户现金、持仓和成交。
4. `BacktestEngine`
   把 K 线一根一根喂给策略，并记录收益。

## 为什么这套更容易看懂

原仓库的设计更接近实盘系统，所以会有：

- CTP 网关
- 异步事件循环
- 本地消息总线
- Django ORM
- 定时任务
- 实盘订单和成交回报

这些在实盘里都合理，但第一次学量化交易时很容易把人绕晕。

极简版先回答最本质的问题：

- 行情从哪里来
- 策略怎么产生日志
- 下单后账户怎么变化
- 最后赚了还是亏了

## 运行

在仓库根目录执行：

```bash
python -m simple_trader.run_demo
```

这是一版“最小回测”，当前是天级别，因为每根 `Bar` 都代表一天。

建议把当前三个 demo 理解成三层：

- `run_demo.py`
  最小教学版，只为看懂量化系统闭环
- `run_futures_demo.py`
  单合约期货教学 / 验证版，方便理解期货语义和调试
- `run_portfolio_demo.py`
  当前主线版本，后续优先在这里继续演进

## 期货仿真 v2

如果你想看一个更接近国内期货系统的版本，可以运行：

```bash
python -m simple_trader.run_futures_demo
```

这一版新增了：

- 合约参数 `ContractSpec`
- 合约乘数
- 保证金比例
- 每手手续费
- 做多和做空
- 从 CSV 读取日线数据

对应文件：

- [futures.py](/Users/bytedance/Desktop/code/trader/simple_trader/futures.py)
- [futures_strategy.py](/Users/bytedance/Desktop/code/trader/simple_trader/futures_strategy.py)
- [data.py](/Users/bytedance/Desktop/code/trader/simple_trader/data.py)
- [run_futures_demo.py](/Users/bytedance/Desktop/code/trader/simple_trader/run_futures_demo.py)

## 多合约组合版

如果你想看“一个账户同时交易多个期货合约”的版本，可以运行：

```bash
python -m simple_trader.run_portfolio_demo
```

这一版新增了：

- 一个组合账户里支持多个合约
- 每个合约可以有自己的 `ContractSpec`
- 组合级资金、权益、保证金统计
- 按交易日依次推进多个合约
- 组合级风控
- 按风险预算自动计算手数
- 组合汇总视图
- 固定百分比止损

## Broker 角色分层

为了以后从回测平滑走到仿真和实盘，现在已经把 broker 角色单独拎出来了：

- [brokers.py](/Users/bytedance/Desktop/code/trader/simple_trader/brokers.py)
  定义了 `BacktestBroker`、`PaperBroker`、`LiveBroker` 三种角色骨架
- [execution.py](/Users/bytedance/Desktop/code/trader/simple_trader/execution.py)
  定义了统一的 `OrderIntent` 和 `ExecutionReport`

当前状态是：

- `FuturesBroker` 和 `PortfolioBroker`
  已经明确归类为 `BacktestBroker`
- `PaperBroker`
  已经支持第一版 `paper matching`，会按最新价格和滑点规则做本地模拟成交
- `LiveBroker`
  已经有最小骨架，但还没接真实 CTP / 券商网关

也就是说，这一层现在已经从“只有想法”变成“代码里真的有三种角色”，后面继续往实盘走会顺很多。

如果你想看 `paper trading` 的最小版本，可以运行：

```bash
python3 -m simple_trader.run_paper_demo
```

这版会：

- 用一条条新到的价格更新 `PaperBroker`
- 让策略在“更像实时”的节奏下发出信号
- 用 `paper matching` 在本地立即模拟成交
- 更新 paper 账户的现金、权益、保证金和持仓

## 一根 K 线来了之后会发生什么

1. 引擎取到新的 `Bar`
2. 把目前为止的历史 K 线交给策略
3. 策略返回 `BUY`、`SELL` 或 `None`
4. `Broker` 更新现金和持仓
5. 引擎记录权益曲线

## 这套代码怎么升级成真实交易系统

当你看懂这套以后，再往上加东西：

1. 把 `demo_bars()` 换成 CSV/数据库行情
2. 把 `Broker` 换成“模拟撮合器”
3. 再把 `Broker` 换成“真实券商/CTP 下单接口”
4. 最后再加日志、风控、数据库、Web 页面

这才是比较自然的学习顺序。
