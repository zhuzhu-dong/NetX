import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { test } from 'node:test'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const packageJson = JSON.parse(read('../package.json'))
const lockfile = read('../package-lock.json')
const dockerfile = read('../Dockerfile')
const nginx = read('../nginx.production.conf')
const healthcheck = read('../healthcheck.sh')
const productionSmoke = read('../../scripts/verify_frontend_production.sh')
const compose = read('../../docker-compose.app.yml')
const overview = read('../src/views/dashboard/NetworkOverview.vue')
const lineArea = read('../src/components/charts/LineAreaChart.vue')
const donut = read('../src/components/charts/DonutChart.vue')

test('production package declares ECharts dependencies', () => {
  assert.ok(packageJson.dependencies.echarts)
  assert.ok(packageJson.dependencies['vue-echarts'])
})

test('lockfile pins ECharts dependencies', () => {
  assert.match(lockfile, /"node_modules\/echarts"/)
  assert.match(lockfile, /"node_modules\/vue-echarts"/)
})

test('production Dockerfile builds with npm ci and serves only dist through Nginx', () => {
  assert.match(dockerfile, /FROM node:20-alpine AS build/)
  assert.match(dockerfile, /RUN npm ci --no-audit --no-fund/)
  assert.match(dockerfile, /RUN npm run build/)
  assert.match(dockerfile, /FROM nginx:alpine AS runtime/)
  assert.match(dockerfile, /COPY --from=build \/app\/dist \/usr\/share\/nginx\/html/)
  assert.match(dockerfile, /frontend-healthcheck/)
  assert.doesNotMatch(dockerfile, /npx vite|npm run dev/)
})

test('production healthcheck rejects Vite entries and fetches built assets', () => {
  assert.match(healthcheck, /grep -q '\/assets\//)
  assert.match(healthcheck, /! grep -qE '\/@vite\/client\|\/src\/main/)
  assert.match(healthcheck, /for asset in \$assets/)
  assert.match(healthcheck, /wget -q -O \/dev/)
})

test('deployment smoke strips HTML attribute delimiters before fetching assets', () => {
  assert.match(productionSmoke, /s\/\^\[a-z\]\+="\/\/; s\/"\$\//)
  assert.match(productionSmoke, /for attempt in \$\(seq 1 10\)/)
  assert.match(productionSmoke, /\/dashboard\/topology/)
  assert.match(productionSmoke, /\/dashboard\/runtime/)
})

test('static server has production cache rules and Vue Router fallback', () => {
  assert.match(nginx, /listen 5173;/)
  assert.match(nginx, /try_files \$uri \$uri\/ \/index\.html;/)
  assert.match(nginx, /location \/assets\//)
  assert.match(nginx, /max-age=31536000, immutable/)
  assert.match(nginx, /location = \/index\.html/)
  assert.match(nginx, /no-cache, no-store, must-revalidate/)
})

test('compose uses the immutable frontend image without source or node_modules mounts', () => {
  const service = compose.slice(compose.indexOf('  idn-frontend:'), compose.indexOf('\nnetworks:'))
  assert.match(service, /build:\s*\n\s+context: \./)
  assert.match(service, /dockerfile: frontend\/Dockerfile/)
  assert.match(service, /image: idn-system-frontend:0810-p1/)
  assert.doesNotMatch(service, /node:20-alpine|working_dir:|npx vite|\.\/frontend:\/app|idn-frontend-node-modules/)
})

test('NetworkOverview and chart source retain their real ECharts integrations', () => {
  assert.match(overview, /from 'echarts'/)
  assert.match(lineArea, /from 'vue-echarts'/)
  assert.match(donut, /from 'vue-echarts'/)
})

test('dist, when built, is a production asset bundle without Vite entries', () => {
  const indexPath = new URL('../dist/index.html', import.meta.url)
  if (!existsSync(indexPath)) return
  const index = read('../dist/index.html')
  assert.match(index, /\/assets\//)
  assert.doesNotMatch(index, /\/@vite\/client|\/src\/main\.js/)
})
