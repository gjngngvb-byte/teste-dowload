export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Método não permitido." });
  }
  const apiKey = process.env.VIDEOFETCH_API_KEY;
  if (!apiKey) return res.status(503).json({ error: "A API ainda não foi configurada. Adicione VIDEOFETCH_API_KEY nas variáveis de ambiente do deploy." });
  const { url, format = "360p" } = req.body || {};
  let parsed;
  try { parsed = new URL(url); } catch { return res.status(400).json({ error: "URL inválida." }); }
  if (!["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"].includes(parsed.hostname)) {
    return res.status(400).json({ error: "Por enquanto, são aceitos apenas links do YouTube." });
  }
  if (!["mp3", "360p", "720p", "1080p"].includes(format)) {
    return res.status(400).json({ error: "Formato não suportado." });
  }
  try {
    const upstream = await fetch("https://api.vidfetch.dev/v1/downloads", {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ url: parsed.toString(), format })
    });
    const data = await upstream.json().catch(() => ({}));
    if (!upstream.ok) {
      return res.status(upstream.status >= 400 && upstream.status < 500 ? upstream.status : 502)
        .json({ error: data.error || data.message || `VidFetch respondeu com HTTP ${upstream.status}.` });
    }
    return res.status(upstream.status === 202 ? 202 : 200).json(data);
  } catch {
    return res.status(502).json({ error: "Não foi possível contactar a VidFetch. Tente novamente." });
  }
}