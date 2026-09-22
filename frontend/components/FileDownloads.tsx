"use client";

import React from "react";
import { BASE } from "../lib/api";

type ToolResult = { name: string; output?: string; running?: boolean };
type FileResult = { name: string; path: string; download_url: string };

export function backendFileHref(url: string): string | null {
  try {
    const parsed = new URL(url, typeof window !== "undefined" ? window.location.href : "http://localhost:3000");
    const base = BASE.replace(/\/$/, "");
    const prefix = base.endsWith("/api") ? base.slice(0, -4) : base;

    // Signed file-search, editor and image-browser download tokens.
    if ((/\/api\/files\/download\/[A-Za-z0-9_.-]+$/.test(parsed.pathname)
        || /\/api\/editor\/download\/[A-Za-z0-9_.-]+$/.test(parsed.pathname)
        || /\/api\/images\/file\/[A-Za-z0-9_.-]+$/.test(parsed.pathname))
        && !parsed.username && !parsed.password && !parsed.search && !parsed.hash) {
      return parsed.origin ? `${prefix}${parsed.pathname}` : `${prefix}${parsed.pathname}`;
    }

    // Obsidian attachment URLs served by the vault.
    if (parsed.pathname === "/api/obsidian/file" && parsed.searchParams.has("path")) {
      const path = parsed.searchParams.get("path") || "";
      return `${prefix}/api/obsidian/file?path=${encodeURIComponent(path)}`;
    }
  } catch {
    // ignore malformed URLs
  }
  return null;
}

export function downloadHref(url: string): string | null {
  const href = backendFileHref(url);
  if (!href) return null;
  try {
    const parsed = new URL(href, typeof window !== "undefined" ? window.location.href : "http://localhost:3000");
    return /\/api\/files\/download\//.test(parsed.pathname) ? href : null;
  } catch {}
  return null;
}

export function filesFromTools(tools: ToolResult[] = []): FileResult[] {
  const files = new Map<string, FileResult>();
  for (const tool of tools) {
    if (tool.name !== "file_search" || !tool.output) continue;
    try {
      const result = JSON.parse(tool.output);
      if (!Array.isArray(result?.files)) continue;
      for (const file of result.files) {
        if (typeof file?.name === "string" && typeof file.path === "string"
            && typeof file.download_url === "string" && downloadHref(file.download_url)) {
          files.set(file.path, file);
        }
      }
    } catch {
      // Tool errors are plain text, not downloadable file results.
    }
  }
  return [...files.values()];
}

export default function FileDownloads({ tools }: { tools?: ToolResult[] }) {
  const files = filesFromTools(tools);
  if (!files.length) return null;
  return (
    <div aria-label="File downloads" style={{ marginTop: 8, maxWidth: "92%", display: "grid", gap: 6 }}>
      {files.map((file) => (
        <div key={file.path}>
          <a href={downloadHref(file.download_url)!} download={file.name}
            style={{ color: "#00e5ff", textDecoration: "underline", overflowWrap: "anywhere" }}>
            Download {file.name}
          </a>
          <div style={{ fontSize: 10, opacity: 0.7, overflowWrap: "anywhere" }}>{file.path}</div>
        </div>
      ))}
    </div>
  );
}
