export type DigestFile = (file: File) => Promise<string>

export async function sha256File(file: File): Promise<string> {
  const content = await file.arrayBuffer()
  const digest = await globalThis.crypto.subtle.digest("SHA-256", content)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("")
}
