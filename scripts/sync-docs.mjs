import { copyFile, mkdir, readdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'

const root = path.resolve(import.meta.dirname, '..')
const docs = path.join(root, 'docs')
const publicDir = path.join(docs, 'public')

const project = await readFile(path.join(root, 'PROJECT.md'), 'utf8')
const projectForDocs = project.replace(
  /```mermaid[\s\S]*?```/,
  '![框架图](/framework.svg)',
)
await writeFile(path.join(docs, 'project.md'), projectForDocs, 'utf8')

for (const [source, target] of [
  ['LOG.md', 'log.md'],
  ['SOP.md', 'sop.md'],
]) {
  await copyFile(path.join(root, source), path.join(docs, target))
}

await mkdir(publicDir, { recursive: true })
await copyFile(path.join(root, 'assets', 'framework.svg'), path.join(publicDir, 'framework.svg'))

const showcaseSource = path.join(root, 'showcase')
const showcaseTarget = path.join(publicDir, 'showcase')
await mkdir(showcaseTarget, { recursive: true })
for (const name of await readdir(showcaseSource)) {
  await copyFile(path.join(showcaseSource, name), path.join(showcaseTarget, name))
}

console.log('docs synced: project.md, log.md, sop.md, public/framework.svg, public/showcase/')
