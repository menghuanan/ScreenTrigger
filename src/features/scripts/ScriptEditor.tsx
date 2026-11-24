/**
 * 脚本编辑器（TASK-012）：源码编辑 + 参数编辑 + 仅编译语法诊断（行/列）。
 *
 * - 草稿由 `ScriptStore` 持有（`draft`）；组件经 `useScriptStore` 订阅；
 * - 参数五字段（name/type/required/default/description）：`required=true` 时 `default`
 *   强制 `null`（02）；`default` 以 JSON 文本编辑（合法 JSON 解析为结构化值，否则保留文本）；
 * - 诊断展示：`line`（自 1 起）+ `column`（`int≥1` 或 `null`——`null` 表示引擎无法无歧义
 *   定位列，**不编造**）；`ENGINE_UNAVAILABLE`（引擎未装 Lua 编译器）经 `state.error` 诚实提示；
 * - 保存携带 `revision` 乐观锁（更新），`CONFLICT` 经 `state.conflict` 提示重试。
 */

import { toUserMessage } from "../../lib/ipc/engineClient";
import type { ScriptParameter } from "../../lib/types/conditions";
import {
  isSourceWithinBudget,
  isValidName,
  nameCodepointLength,
  sourceByteLength,
  NAME_MAX_CODEPOINTS,
  SOURCE_MAX_BYTES,
} from "./api";
import { useScriptStore } from "./store";
import type { ScriptStore } from "./store";

interface ScriptEditorProps {
  store: ScriptStore;
}

const PARAM_TYPES: readonly ScriptParameter["type"][] = [
  "string",
  "number",
  "boolean",
  "object",
  "array",
];

/** `default` JSON 文本 → 结构化值（空 → null；非法 JSON → 原文本）。 */
function parseDefault(text: string): unknown {
  if (text.trim() === "") {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** `default` → 可编辑 JSON 文本。 */
function stringifyDefault(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

export default function ScriptEditor({ store }: ScriptEditorProps) {
  const state = useScriptStore(store);
  const draft = state.draft;
  const isUpdate = draft.id !== null && draft.revision !== null;

  const replaceParameter = (index: number, next: ScriptParameter): void => {
    const parameters = draft.parameters.map((item, i) => (i === index ? next : item));
    store.updateDraft({ parameters });
  };

  const addParameter = (): void => {
    const parameters: ScriptParameter[] = [
      ...draft.parameters,
      { name: "", type: "string", required: false, default: null, description: "" },
    ];
    store.updateDraft({ parameters });
  };

  const removeParameter = (index: number): void => {
    store.updateDraft({ parameters: draft.parameters.filter((_, i) => i !== index) });
  };

  return (
    <section className="script-editor" aria-label="脚本编辑器">
      <h2>{isUpdate ? `编辑脚本（revision ${draft.revision}）` : "新建脚本"}</h2>

      <div className="script-editor-toolbar">
        <button type="button" onClick={() => store.startCreate()} disabled={state.busy}>
          新建
        </button>
        <button type="button" onClick={() => void store.validate()} disabled={state.busy}>
          语法校验
        </button>
        <button type="button" onClick={() => void store.save()} disabled={state.busy}>
          保存
        </button>
      </div>

      {state.error !== null && (
        <p role="alert">
          {state.conflict
            ? "版本冲突：请重新加载后再试。"
            : state.error.code === "ENGINE_UNAVAILABLE"
              ? "本机引擎未安装 Lua 编译器（lupa），无法进行语法校验。"
              : `操作失败：${toUserMessage(state.error)}`}
        </p>
      )}

      <label>
        名称
        <input
          type="text"
          value={draft.name}
          onChange={(event) => store.updateDraft({ name: event.target.value })}
        />
      </label>
      {!isValidName(draft.name) && (
        <p role="status">
          名称须为 1–{NAME_MAX_CODEPOINTS} 个字符（当前 {nameCodepointLength(draft.name.trim())}）
        </p>
      )}

      <label>
        源码（Lua）
        <textarea
          rows={12}
          value={draft.source}
          onChange={(event) => store.updateDraft({ source: event.target.value })}
        />
      </label>
      <p role="status">
        {sourceByteLength(draft.source)} / {SOURCE_MAX_BYTES} 字节
        {!isSourceWithinBudget(draft.source) && "（超出上限）"}
      </p>

      <fieldset>
        <legend>参数</legend>
        <button type="button" onClick={addParameter} disabled={state.busy}>
          添加参数
        </button>
        <table>
          <thead>
            <tr>
              <th scope="col">name</th>
              <th scope="col">type</th>
              <th scope="col">required</th>
              <th scope="col">default</th>
              <th scope="col">description</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {draft.parameters.map((param, index) => (
              <tr key={index}>
                <td>
                  <input
                    type="text"
                    value={param.name}
                    onChange={(event) =>
                      replaceParameter(index, { ...param, name: event.target.value })
                    }
                  />
                </td>
                <td>
                  <select
                    value={param.type}
                    onChange={(event) =>
                      replaceParameter(index, {
                        ...param,
                        type: event.target.value as ScriptParameter["type"],
                      })
                    }
                  >
                    {PARAM_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={param.required}
                    onChange={(event) =>
                      replaceParameter(index, {
                        ...param,
                        required: event.target.checked,
                        // required=true ⇒ default 必须 null（02）
                        default: event.target.checked ? null : param.default,
                      })
                    }
                  />
                </td>
                <td>
                  <input
                    type="text"
                    value={stringifyDefault(param.default)}
                    disabled={param.required}
                    onChange={(event) =>
                      replaceParameter(index, {
                        ...param,
                        default: parseDefault(event.target.value),
                      })
                    }
                  />
                </td>
                <td>
                  <input
                    type="text"
                    value={param.description}
                    onChange={(event) =>
                      replaceParameter(index, { ...param, description: event.target.value })
                    }
                  />
                </td>
                <td>
                  <button type="button" onClick={() => removeParameter(index)} disabled={state.busy}>
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </fieldset>

      {state.validation !== null && (
        <div className="script-editor-diagnostics" aria-live="polite">
          {state.validation.valid ? (
            <p>语法校验通过。</p>
          ) : (
            <ul>
              {state.validation.diagnostics.map((diagnostic, index) => (
                <li key={index}>
                  第 {diagnostic.line} 行
                  {diagnostic.column !== null ? `，第 ${diagnostic.column} 列` : "（列未知）"}
                  ：{diagnostic.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
