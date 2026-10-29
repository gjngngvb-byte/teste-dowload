export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    return res.status(405).json({ error: "Método não permitido." });
  }
  const apiKey = process.env.VIDEOFETCH_API_KEY;
  if (!apiKey) return res.status(503).json({ error: "A API ainda não foi configurada. Adicione VIDEOFETCH_API_KEY nas variáveis de ambiente do deploy." });
  const id = typeof req.query.id === "string" ? req.query.id : "";
  if (!/^dl_[a-zA-Z0-9_-]{1,100}$/.test(id)) return res.status(400).json({ error: "ID de trabalho inválido." });
  try {
    const upstream = await fetch(`https://api.vidfetch.dev/v1/downloads/${encodeURIComponent(id)}`, {
      headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" }
    });
    const data = await upstream.json().catch(() => ({}));
    if (!upstream.ok) return res.status(upstream.status >= 400 && upstream.status < 500 ? upstream.status : 502)
      .json({ error: data.error || data.message || `VidFetch respondeu com HTTP ${upstream.status}.` });
    return res.status(200).json(data);
  } catch {
    return res.status(502).json({ error: "Não foi possível consultar o estado na VidFetch." });
  }
}