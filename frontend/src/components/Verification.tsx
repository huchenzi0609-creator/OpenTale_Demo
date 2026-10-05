import type { Verification as VerificationResult } from "../types";
export default function Verification({
  verification,
  onClose,
}: {
  verification: VerificationResult;
  onClose: () => void;
}) {
  return (
    <section className="panel verification" aria-label="离线验证结果">
      <div className="section-header">
        <div>
          <p className="eyebrow">VERIFICATION / DETERMINISTIC KERNEL</p>
          <h2>{verification.ok ? "✓ 离线机制验证通过" : "离线验证发现问题"}</h2>
        </div>
        <button className="text-button" onClick={onClose}>
          收起
        </button>
      </div>
      <ul>
        {verification.checks.map((check) => (
          <li className={check.passed ? "passed" : "failed"} key={check.name}>
            <span>{check.passed ? "✓" : "×"}</span>
            <div>
              <strong>{check.name}</strong>
              <p>{check.detail}</p>
            </div>
          </li>
        ))}
      </ul>
      <p className="helper">{verification.note}</p>
    </section>
  );
}
