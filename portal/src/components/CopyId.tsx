// Truncated, click-to-copy identifier. IDs like pj_db1f606b9036 dominate table
// width; this shows a short form and copies the full value on click with a
// 1.5s "Copied" flash. Falls back to plain text if clipboard is blocked.
import { useState } from "react";

export function CopyId({ value, prefix }: { value: string; prefix?: string }) {
  const [copied, setCopied] = useState(false);
  const label = prefix ? `${prefix}${value}` : value;
  return (
    <button
      type="button"
      className="copy-id"
      title={value}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard blocked — title still shows full value */
        }
      }}
    >
      <code className="id-cell">{copied ? "Copied" : label}</code>
    </button>
  );
}
