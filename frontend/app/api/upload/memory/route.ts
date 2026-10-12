import { forwardUpload } from "../proxy";
import type { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  return forwardUpload(req, "/api/memory/upload");
}