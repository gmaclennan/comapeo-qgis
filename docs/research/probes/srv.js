const http = require('node:http')
const fs = require('node:fs')
const sock = process.argv[2]
try { fs.unlinkSync(sock) } catch {}
const handler = (req, res) => {
  if (req.url.endsWith('.svg')) {
    res.writeHead(200, { 'content-type': 'image/svg+xml' })
    res.end('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><circle cx="5" cy="5" r="4" fill="red"/></svg>')
  } else {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ ok: true, url: req.url, via: req.socket.localPort ? 'tcp' : 'uds' }))
  }
}
http.createServer(handler).listen(sock, () => console.log('uds up'))
http.createServer(handler).listen(47911, '127.0.0.1', () => console.log('tcp up'))
setTimeout(() => process.exit(0), 60000)
