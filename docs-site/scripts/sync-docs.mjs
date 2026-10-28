import { copyFile, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'

// docs-site 是附属模块：只从项目根读取文档源与产物，不反向写入
const site = path.resolve(import.meta.dirname, '..')
const root = path.resolve(site, '..')
const publicDir = path.join(site, 'public')
const run = promisify(execFile)

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

// 提交记录：git 记事实，LOG.md 记人话，两者互补
const commitsNote = [
  '# 提交记录',
  '',
  '> 由 `git log` 自动生成，每次构建刷新。按版本讲人话的说明见 [开发记录](/log)。',
  '',
]
try {
  const { stdout } = await run('git', [
    '-C',
    root,
    'log',
    '-n',
    '80',
    '--date=format:%Y-%m-%d %H:%M',
    '--pretty=format:%h%x09%ad%x09%s',
  ])
  const rows = stdout
    .split('\n')
    .filter(Boolean)
    .map((line) => line.split('\t'))
    .filter((parts) => parts.length === 3)
    .map(([hash, date, subject]) => {
      const [day, time] = date.split(' ')
      return { hash, day, time, subject }
    })

  const byDay = new Map()
  for (const row of rows) {
    if (!byDay.has(row.day)) byDay.set(row.day, [])
    byDay.get(row.day).push(row)
  }

  for (const [day, items] of byDay) {
    commitsNote.push(`## ${day}`, '')
    for (const item of items) {
      commitsNote.push(`- \`${item.hash}\` ${item.time} — ${item.subject}`)
    }
    commitsNote.push('')
  }
  commitsNote.push('> 只展示最近 80 条。分支与合并关系用 Git Graph 类插件查看。', '')
} catch {
  commitsNote.push('> 生成失败：当前环境无法读取 git 历史（未安装 git 或不在仓库内）。', '')
}
await writeFile(path.join(site, 'commits.md'), commitsNote.join('\n'), 'utf8')
console.log('docs synced: commits.md（最近 80 条提交）')
