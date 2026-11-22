import { useEffect, useState } from "react";
import { api } from "../api";

export default function Home() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .总览()
      .then(setD)
      .catch((e) => setErr(e.message));
  }, []);

  const cells = d
    ? [
        ["在途水位", d.在途水位],
        ["待取", d.待取],
        ["隔离未解决", d.隔离未解决],
        ["其中丢失未解决", d.丢失隔离未解决],
        ["今日已取", d.已取],
        ["日闸", d.日闸],
      ]
    : [];

  return (
    <>
      <div className="hero-strip">
        <section className="ticket-hero">
          <h2>今日挂衣</h2>
          <p>
            票墙左侧是工位入口。先开票贴号，再沿工序推到待取，顾客凭暗码核销；逾承诺日可进超期隔离。
            丢失即终态：进入丢失隔离后取衣、推进、再隔离一律硬拒，认领仅登记审计、不回改票态；丢失按损失出途，不计在途水位。
          </p>
        </section>
        <div className="metrics">
          {cells.map(([k, v], i) => (
            <div className="metric" key={k} style={{ animationDelay: `${i * 0.05}s` }}>
              <b>{v ?? "—"}</b>
              <span>{k}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="msg">{err}</div>
      {d && (
        <section className="panel">
          <h3>工序分布</h3>
          <table>
            <thead>
              <tr>
                <th>状态</th>
                <th>票数</th>
              </tr>
            </thead>
            <tbody>
              {["已收", "水洗", "烘干", "熨烫", "待取", "已取", "超期隔离", "丢失隔离"].map(
                (s) => (
                  <tr key={s}>
                    <td>
                      <span className={`badge ${s}`}>{s}</span>
                    </td>
                    <td>{d[s]}</td>
                  </tr>
                )
              )}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
