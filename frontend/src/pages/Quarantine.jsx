import { useEffect, useState } from "react";
import { api } from "../api";

const LOST = "丢失隔离";

export default function Quarantine() {
  const [ready, setReady] = useState([]);
  const [logs, setLogs] = useState([]);
  const [stats, setStats] = useState(null);
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState({});

  async function load() {
    const [a, b, u] = await Promise.all([
      api.挂牌列表("?status=" + encodeURIComponent("待取")),
      api.隔离列表(),
      api.未解决数(),
    ]);
    setReady(a);
    setLogs(b);
    setStats(u);
  }

  useEffect(() => {
    load().catch((e) => setMsg(e.message));
  }, []);

  async function iso(ticket_no, kind) {
    setMsg("");
    try {
      await api.隔离(ticket_no, {
        kind,
        reason: kind === "超期隔离" ? "逾承诺日未取" : "店内申报丢失",
        operator: "店长",
      });
      setMsg(`${ticket_no} → ${kind}`);
      await load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function claim(ticket_no) {
    setMsg("");
    const f = form[ticket_no] || {};
    const claimant = (f.claimant || "").trim();
    if (!claimant) {
      setMsg(`${ticket_no} 认领需填写认领人`);
      return;
    }
    try {
      await api.认领({
        ticket_no,
        claimant,
        contact: (f.contact || "").trim(),
        evidence: (f.evidence || "").trim(),
        note: (f.note || "").trim(),
        operator: "店长",
      });
      setMsg(`${ticket_no} 认领已登记（仅留痕，票籍保持「${LOST}」终态）`);
      setForm({ ...form, [ticket_no]: {} });
      await load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  const setField = (no, k, v) =>
    setForm((prev) => ({ ...prev, [no]: { ...(prev[no] || {}), [k]: v } }));

  return (
    <>
      <section className="panel">
        <h3>待取转隔离</h3>
        <p className="msg">{msg}</p>
        <p className="hint">
          口径：丢失即终态。进入「{LOST}」后取衣/推进/再隔离一律硬拒；认领只登记审计，不回改票态、不关闭隔离行。
        </p>
        <table>
          <thead>
            <tr>
              <th>票号</th>
              <th>承诺日</th>
              <th>顾客</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {ready.map((r) => (
              <tr key={r.ticket_no}>
                <td>{r.ticket_no}</td>
                <td>{r.due_date}</td>
                <td>{r.customer_name}</td>
                <td>
                  <div className="row">
                    <button type="button" onClick={() => iso(r.ticket_no, "超期隔离")}>
                      超期隔离
                    </button>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => iso(r.ticket_no, LOST)}
                    >
                      丢失隔离
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3>
          隔离流水（未解决 {stats?.未解决数 ?? "—"} ｜ 丢失 {stats?.丢失隔离未解决 ?? "—"} ｜
          超期 {stats?.超期隔离未解决 ?? "—"}）
        </h3>
        <table>
          <thead>
            <tr>
              <th>票号</th>
              <th>种类</th>
              <th>原因</th>
              <th>时间</th>
              <th>隔离行</th>
              <th>当前票态</th>
              <th>认领登记（仅留痕）</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((r) => {
              const f = form[r.ticket_no] || {};
              const isOpenLost = r.kind === LOST && r.resolved === 0;
              return (
                <tr key={r.id}>
                  <td>{r.ticket_no}</td>
                  <td>
                    <span className={`badge ${r.kind}`}>{r.kind}</span>
                  </td>
                  <td>{r.reason}</td>
                  <td>{r.created_at}</td>
                  <td>
                    <span className={`badge ${r.resolved ? "已取" : "丢失隔离"}`}>
                      {r.状态}
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${r.ticket_status}`}>{r.ticket_status}</span>
                  </td>
                  <td>
                    {isOpenLost ? (
                      <div className="claim-box">
                        <input
                          placeholder="认领人*"
                          value={f.claimant || ""}
                          onChange={(e) => setField(r.ticket_no, "claimant", e.target.value)}
                        />
                        <input
                          placeholder="联系方式"
                          value={f.contact || ""}
                          onChange={(e) => setField(r.ticket_no, "contact", e.target.value)}
                        />
                        <input
                          placeholder="凭证（可选）"
                          value={f.evidence || ""}
                          onChange={(e) => setField(r.ticket_no, "evidence", e.target.value)}
                        />
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => claim(r.ticket_no)}
                        >
                          登记认领
                        </button>
                      </div>
                    ) : (
                      <span className="muted-text">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </>
  );
}
