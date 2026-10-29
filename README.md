# WenFetch · teste da API VidFetch

Protótipo de site estático com duas funções serverless Vercel que fazem proxy para a API VidFetch. A chave fica no servidor e não é enviada ao navegador.

## Configuração

1. Crie uma chave de API em https://www.vidfetch.dev/.
2. No projeto Vercel **wenfetch**, abra **Settings → Environment Variables**.
3. Adicione `VIDEOFETCH_API_KEY` com a sua chave para Production (e Preview, se quiser testar previews).
4. Faça um novo deploy.
5. Abra o site, cole um URL público do YouTube e selecione MP4 ou MP3.

## Endpoints internos

- `POST /api/download` com JSON `{"url":"https://www.youtube.com/watch?v=...","format":"360p"}`
- `GET /api/status?id=dl_...`

O backend chama `POST https://api.vidfetch.dev/v1/downloads` e consulta `GET /v1/downloads/{id}`.

## Limites

O plano gratuito da VidFetch anuncia 1 GB de tráfego por mês; não significa 1.000 downloads garantidos. A qualidade 720p/1080p pode depender da conta. Links de download são temporários. Use somente conteúdo que você tenha autorização para baixar.

## Observações de teste

Este é um protótipo, não um serviço pronto para tráfego público. Antes de o divulgar, adicione proteção contra abuso/rate limiting às funções serverless. Nunca coloque a chave VidFetch no HTML ou no JavaScript do navegador.
