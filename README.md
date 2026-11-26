# 联作工艺品的分件验收、领货与尾款

分件名称指实物，组合依赖来自订单约定。价款以分计。领取人的授权与合作取件点的实物收据分别记录。

`fixtures/registers.json` 提供演练台账，`fixtures/events.ndjson` 提供各端送达的独立消息，`fixtures/receipts.json` 提供合作方的模拟回执。事件游标为送达顺序。业务发生时点出现在消息内容中时，以其来源所注明的时点为准。整套资料均为合成数据。

## 本地联调

使用 Python 3.11.2 标准库。联调入口只监听本地地址，不需要账号或外部连接。

```sh
python3 -m unittest -v
python3 fixture_gateway.py --port 8765
```

`GET /v1/registers` 返回台账。用 `GET /v1/events?after=0&limit=50` 读取一页消息，后续页将上一页的 `next_cursor` 作为 `after`。每条消息有 `cursor`、`source`、`minute` 及原业务记录 `payload`。

对端发送协议为 `POST /v1/receipts`，正文提供 `reference`、`operation`、`recipient`，三个字段均为字符串。需要配对的操作与接收方见回执示例文件。返回对象含 `reference` 和对端 `receipt`。若条目设置 `lost_reply`，首次调用被记录后连接断开。可用 `GET /v1/receipts/<reference>` 查看已记录的回执。相同编号和内容可重试，编号被另一个操作使用时返回 409。

联调进程运行期间保存回执记录。需要试读其他台账、消息与回执时，可通过 `--fixtures` 指向另一组同名资料文件。合作方只返回模拟事实，业务办理功能由各方应用负责。

## 分件验收、领货与结算服务

`cooperative.py` 实现持续办理的业务服务（纯标准库、纯领域逻辑），`counterparty.py` 是合作取件点与资金经办的回执端口：

* `FakeReceiptPort`：测试用的内存桩，可模拟正常回执、断线、一直查不到、对方称未收到。
* `HttpReceiptPort`：对接上面的本地联调入口，遵守同编号幂等重试、编号他用 409、断线保留首记的协议。

### 业务口径

* **样稿变更**只重开受影响且尚未确认的分件；已独立验收的实物保留，拒收只把责任分件打回原料/补做。组合依赖未全部通过时，是否可独立领货取决于客人是否允许拆领及该件有无领取人。
* **补做与换料**不改变约定价款；任何涨价都必须取得客人明确同意。带序列号的共用底座若已确认给另一订单，本订单不得挤占。
* **拆领授权**按分件授予，未确认收到的分件不能同时交给两个领取人。授权可拆换，但断线后待核实的交接仍绑定原凭据与原领取人，不会因此被重新标为可交。
* **断线交接**记为 `pending_verification`，保管方继续承担核对责任；只有同一张交接凭据能查清该件（`reconcile_handover` / 通用 `reconcile`），对方称未收到则进入 `disputed`。
* **尾款**只能对已验收且已核实收到的分件释放（`release_payment`），未核实交割在款项经办视图中显式阻断。
* **退改结算**（`open_change_case`）按各分件约定价款、订金 FIFO 冲抵额与此前已付记录生成行项：退件退其名下已付，已签收且未退回的实物保留应付部分，同一客人的退款与应付轧差；退款经资金经办端口划出，断线可凭同一编号重试。
* 查询视图：`maker_view` 看分件卡在原料/验收/领货/结算哪一道，`custody_view` 看待核实交接，`payment_view` 看可释放尾款、阻断项、未核实交割与待划退款，`statement` 出订单级结算单。

### 运行

```sh
python3 -m unittest -v
# 离线演练：只应用台账与消息，断线交接留待核实
python3 cooperative.py
# 对接真实本地联调入口：交接与退款回执走 HTTP 核对
python3 fixture_gateway.py --port 8765
python3 cooperative.py --gateway-port 8765
```
