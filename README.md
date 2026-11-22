# 衣票墙（hangslip）

收衣贴号 → 工序推进 → 取衣核销关账 → 超期/丢失隔离互证。

- 前端 http://127.0.0.1:6200
- API http://127.0.0.1:11200

## 丢失后票籍口径（全局唯一：架构 A —— 丢失即终态）

挂牌进入「丢失隔离」后，系统只贯彻这一套口径，所有入口（取衣、认领、隔离、挂牌态、事件、水位、隔离页、总览）同源：

- **取衣硬拒**：即使暗码正确也返回 409，杜绝「丢失票仍能暗码取衣」。
- **推进硬拒 / 再隔离硬拒**：丢失是不可逆终态。
- **认领只留审计**：`POST /api/认领` 仅追加 `claims` 审计行与「认领登记」事件，
  **不回改挂牌态、不关闭丢失隔离行**；认领须填认领人，凭证可选。因此不存在「半截认领」。
- **隔离行与票态同成败**：超期转丢失时在同一事务内关闭旧隔离行、开新行；
  部分唯一索引保证任一挂牌至多一行未关隔离。
- **并发不争双终态**：所有写操作走 `BEGIN IMMEDIATE` 单事务；取衣核销有 `pickups` 唯一约束，
  认领与取衣争用时只可能成立一条终态路径，失败整体回滚。
- **水位**：丢失按损失出途，不计在途；认领不改水位。
- **总览未解决数**：以隔离表未关行为准，与 `/api/隔离/未解决数` 同源；丢失行恒计入。
- **事件可重放**：认领事件 `from_status == to_status == 丢失隔离`，重放不产生任何恢复跳变。

> 本范围只覆盖「丢失后恢复」；超期自动扫描另立需求，不在此实现。

## 测试

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest tests.test_loss_terminal -v
```

本地开发：

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ..\frontend
npm install
npm run dev
```
