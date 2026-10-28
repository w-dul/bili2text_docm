import { copyFile, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'

// docs-site 是附属模块：只从项目根读取文档源与产物，不反向写入
const site = path.resolve(import.meta.dirname, '..')
const root = path.resolve(site, '..')
const publicDir = path.join(site, 'public')

const project = await readFile(path.join(root, 'PROJECT.md'), 'utf8')
const projectForDocs = project.replace(
  /```mermaid[\s\S]*?```/,
  '![框架图](./framework.svg)',
)
await writeFile(path.join(site, 'project.md'), projectForDocs, 'utf8')

for (const [source, target] of [
  ['LOG.md', 'log.md'],
  ['SOP.md', 'sop.md'],
]) {
  await copyFile(path.join(root, source), path.join(site, target))
}

// 站点根内的静态资源会由 Vite 处理，自动带上 base
await copyFile(path.join(root, 'assets', 'framework.svg'), path.join(site, 'framework.svg'))

// 展示页是独立 HTML，只能放在 public 下原样发布
const showcaseTarget = path.join(publicDir, 'showcase')
await rm(showcaseTarget, { recursive: true, force: true })
await mkdir(showcaseTarget, { recursive: true })
for (const name of await readdir(path.join(root, 'showcase'))) {
  await copyFile(path.join(root, 'showcase', name), path.join(showcaseTarget, name))
}

console.log('docs synced: project.md, log.md, sop.md, framework.svg, public/showcase/')
