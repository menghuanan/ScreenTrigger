/**
 * 识别结果与证据展示（TASK-016；02 §8 / F03）。
 *
 * - 未命中（`found=false,matches=[]`）是**数据不是错误**：明确展示"未命中"，非报错；
 * - 每个 `Match`：`bbox/center`（含 frame.origin 的绝对坐标）、`confidence`（=锚点分数，
 *   非概率）、`geometry_revision`、`space`；
 * - 证据逐叶：`matched`（命中）/`skipped=true`（短路未执行，**不伪造分数**）/ OCR 文本；
 *   当 `skipped` 时明确标注"未执行（短路）"，不显示被伪造的分数；
 * - `truncated=true` 提示候选超上限（仅由锚点候选上限决定，02）。
 */

import type { EvidenceLeaf, RecognitionMatch, RecognitionResult } from "./recognitionTestClient";

interface EvidenceListProps {
  result: RecognitionResult;
}

export default function EvidenceList({ result }: EvidenceListProps) {
  if (!result.found) {
    return (
      <div className="rt-result rt-miss" aria-label="识别结果">
        <p className="rt-miss-line">
          <strong>未命中</strong>（found=false，matches=[]）——这是正常数据，不是错误。
        </p>
        <p className="muted">
          frame_id：{result.frame_id}；耗时 {result.elapsed_ms}ms
        </p>
      </div>
    );
  }

  return (
    <div className="rt-result" aria-label="识别结果">
      <p className="rt-summary">
        命中 {result.matches.length} 个
        {result.truncated && <span className="rt-truncated">（候选超上限，已截断）</span>}
        ；耗时 {result.elapsed_ms}ms
        {result.target_id !== null && <>；target_id：{result.target_id}</>}
        {result.sample_no !== null && <>；样本号：{result.sample_no}</>}
      </p>
      <ol className="rt-matches">
        {result.matches.map((match, index) => (
          <MatchCard key={`match-${index}`} match={match} index={index} />
        ))}
      </ol>
    </div>
  );
}

function MatchCard({ match, index }: { match: RecognitionMatch; index: number }) {
  return (
    <li className="rt-match">
      <div className="rt-match-head">
        <span className="rt-match-index">#{index + 1}</span>
        <span className="rt-match-bbox">
          bbox ({match.bbox.x}, {match.bbox.y}) {match.bbox.width}×{match.bbox.height}
        </span>
        <span className="rt-match-center">
          center ({match.center.x}, {match.center.y})
        </span>
        <span className="rt-match-confidence">confidence {match.confidence.toFixed(3)}</span>
      </div>
      <div className="rt-match-meta">
        space：{match.space}；geometry_revision：{match.geometry_revision}
      </div>
      {match.evidence.length > 0 && (
        <ul className="rt-evidence">
          {match.evidence.map((leaf) => (
            <EvidenceRow key={leaf.condition_id} leaf={leaf} />
          ))}
        </ul>
      )}
    </li>
  );
}

function EvidenceRow({ leaf }: { leaf: EvidenceLeaf }) {
  if (leaf.skipped) {
    return (
      <li className="rt-evidence-row rt-evidence-skipped">
        <span className="rt-evidence-id">#{leaf.condition_id}</span>
        <span className="rt-evidence-state">未执行（短路）</span>
      </li>
    );
  }
  return (
    <li className={`rt-evidence-row ${leaf.matched === true ? "rt-hit" : "rt-missrow"}`}>
      <span className="rt-evidence-id">#{leaf.condition_id}</span>
      <span className="rt-evidence-state">{leaf.matched === true ? "命中" : "未命中"}</span>
      {leaf.confidence !== null && (
        <span className="rt-evidence-conf">confidence {leaf.confidence.toFixed(3)}</span>
      )}
      {leaf.text !== null && <span className="rt-evidence-text">文本：{leaf.text}</span>}
    </li>
  );
}
