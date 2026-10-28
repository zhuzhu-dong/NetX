/**
 * useTopologyStyle.js — Cytoscape stylesheet
 *
 * Visual reference: ONOS Web UI topology.
 * Clean, professional, information-dense.  Subtle depth, no gaudy effects.
 *
 * Color semantics:
 *   switch  = steel-indigo   host = teal
 *   degraded = amber         failed/attack = rose
 */

// ── Palette ──────────────────────────────────────────────────────────────────
const C = {
  switch:       '#7c7cf0',
  switchBg:     '#e8e8ff',
  switchRgb:    '124,124,240',
  host:         '#0dbbb2',
  hostBg:       '#d6f7f4',
  hostRgb:      '13,187,178',
  degraded:     '#e8962e',
  degradedBg:   '#fef5e7',
  degradedRgb:  '232,150,46',
  failed:       '#e04a5a',
  failedBg:     '#fde8ea',
  failedRgb:    '224,74,90',

  linkNormal:   'rgba(90,106,140,0.38)',
  linkBackup:   'rgba(200,150,60,0.50)',
  linkFailed:   'rgba(210,70,85,0.55)',

  selectRing:   '#3ea8f0',
  highlight:    '#5b5fcf',
}

/* Build a glass-orb background for a node:
   layer 1: wide faint outer glow ring
   layer 2: body fill (soft→accent gradient)
   layer 3: glass highlight (bright spot at upper-left)
*/
function orb(accent, soft, rgb) {
  return [
    // outer glow ring
    `radial-gradient(circle 48% at 50% 50%, rgba(${rgb},0.22) 0%, rgba(${rgb},0.06) 70%, transparent 100%)`,
    // body gradient
    `linear-gradient(160deg, ${soft} 0%, ${accent} 55%, rgba(20,20,40,0.15) 100%)`,
    // glass highlight (upper-left sheen)
    `radial-gradient(ellipse 28% 22% at 32% 28%, rgba(255,255,255,0.88) 0%, transparent 100%)`,
  ].join(',')
}

// ── Layout defaults ──────────────────────────────────────────────────────────
export const DEFAULT_LAYOUT = {
  name: 'cose-bilkent',
  animate: 'during',
  animationDuration: 800,
  fit: true,
  padding: 60,
  randomize: false,
  nodeRepulsion: 10000,
  idealEdgeLength: 140,
  edgeElasticity: 0.32,
  gravity: 0.22,
  numIter: 2000,
  tile: true,
  tilingPaddingVertical: 20,
  tilingPaddingHorizontal: 20,
  gravityRange: 3.5,
}

// ── Style builder ─────────────────────────────────────────────────────────────
export function buildCytoscapeStyle() {
  return [

    // ═══ Switch — rounded rect, glass-orb fill, status-driven border ═════
    {
      selector: 'node[type="switch"]',
      style: {
        width: 52, height: 52,
        shape: 'round-rectangle',
        'border-radius': 10,
        'background-color': C.switch,
        'background-opacity': 0,
        'border-width': 2,
        'border-color': `rgba(${C.switchRgb},0.50)`,
        'border-opacity': 0.7,
        'background-image': orb(C.switch, C.switchBg, C.switchRgb),
        'background-image-opacity': 0.92,
        'background-width': '106%',
        'background-height': '106%',
        'background-position-x': '-3%',
        'background-position-y': '-3%',
        'background-fit': 'cover',
        'shadow-color': `rgba(${C.switchRgb},0.2)`,
        'shadow-blur': 10,
        'shadow-opacity': 0.5,
        'shadow-offset-y': 2,
        'transition-property': 'border-color, border-width, shadow-blur, shadow-opacity',
        'transition-duration': 200,
      },
    },
    {
      selector: 'node[type="switch"][state="degraded"]',
      style: {
        'border-color': `rgba(${C.degradedRgb},0.55)`,
        'background-image': orb(C.degraded, C.degradedBg, C.degradedRgb),
        'shadow-color': `rgba(${C.degradedRgb},0.25)`,
      },
    },
    {
      selector: 'node[type="switch"][state="failed"], node[type="switch"][state="attack"]',
      style: {
        'border-color': `rgba(${C.failedRgb},0.60)`,
        'background-image': orb(C.failed, C.failedBg, C.failedRgb),
        'shadow-color': `rgba(${C.failedRgb},0.3)`,
      },
    },

    // ═══ Host — smaller pill with glass-orb fill ══════════════════════════
    {
      selector: 'node[type="host"]',
      style: {
        width: 44, height: 30,
        shape: 'round-rectangle',
        'border-radius': 10,
        'background-color': C.host,
        'background-opacity': 0,
        'border-width': 2,
        'border-color': `rgba(${C.hostRgb},0.48)`,
        'border-opacity': 0.7,
        'background-image': orb(C.host, C.hostBg, C.hostRgb),
        'background-image-opacity': 0.9,
        'background-width': '108%',
        'background-height': '108%',
        'background-position-x': '-4%',
        'background-position-y': '-4%',
        'background-fit': 'cover',
        'shadow-color': `rgba(${C.hostRgb},0.18)`,
        'shadow-blur': 8,
        'shadow-opacity': 0.4,
        'shadow-offset-y': 1,
        'transition-property': 'border-color, border-width',
        'transition-duration': 200,
      },
    },
    {
      selector: 'node[type="host"][state="degraded"]',
      style: {
        'border-color': `rgba(${C.degradedRgb},0.55)`,
        'background-image': orb(C.degraded, C.degradedBg, C.degradedRgb),
        'shadow-color': `rgba(${C.degradedRgb},0.22)`,
      },
    },
    {
      selector: 'node[type="host"][state="failed"], node[type="host"][state="attack"]',
      style: {
        'border-color': `rgba(${C.failedRgb},0.60)`,
        'background-image': orb(C.failed, C.failedBg, C.failedRgb),
        'shadow-color': `rgba(${C.failedRgb},0.25)`,
      },
    },

    // ═══ Agent — invisible canvas anchor (HTML overlay renders the visual) ═
    {
      selector: 'node[type="agent"]',
      style: {
        width: 100, height: 100,
        shape: 'ellipse',
        'background-color': 'transparent',
        'background-opacity': 0,
        'border-width': 0,
      },
    },

    // ═══ Edges ════════════════════════════════════════════════════════════
    {
      selector: 'edge',
      style: {
        width: 1.6,
        'line-color': C.linkNormal,
        'curve-style': 'bezier',
        'target-arrow-shape': 'none',
        opacity: 0.88,
        'transition-property': 'line-color, width, opacity',
        'transition-duration': 180,
      },
    },
    {
      selector: 'edge[role="backup"], edge[role="secondary"]',
      style: {
        'line-color': C.linkBackup,
        'line-style': 'dashed',
        'line-dash-pattern': [6, 4],
        width: 1.8,
      },
    },
    {
      selector: 'edge[tone="degraded"], edge[state="degraded"]',
      style: {
        'line-color': C.linkBackup,
        'line-style': 'dashed',
        'line-dash-pattern': [6, 4],
        width: 1.8,
      },
    },
    {
      selector: 'edge[tone="failed"], edge[tone="attack"], edge[state="failed"], edge[state="attack"]',
      style: {
        'line-color': C.linkFailed,
        'line-style': 'dashed',
        'line-dash-pattern': [8, 4],
        width: 2,
        opacity: 0.75,
      },
    },

    // ═══ Interaction ══════════════════════════════════════════════════════
    {
      selector: 'node:selected',
      style: {
        'border-width': 3.5,
        'border-color': C.selectRing,
        'border-opacity': 0.9,
        'shadow-color': C.selectRing,
        'shadow-blur': 16,
        'shadow-opacity': 0.4,
      },
    },
    {
      selector: 'node.hover',
      style: {
        'border-width': 2.8,
        'border-opacity': 0.85,
      },
    },
    {
      selector: 'node.dimmed',
      style: { opacity: 0.35 },
    },
    {
      selector: 'edge.dimmed',
      style: { opacity: 0.22 },
    },

    // ═══ Highlight (path / search) ════════════════════════════════════════
    {
      selector: 'node.highlighted',
      style: {
        'border-width': 3,
        'border-color': C.highlight,
        'border-opacity': 0.9,
        'shadow-color': C.highlight,
        'shadow-blur': 18,
        'shadow-opacity': 0.5,
      },
    },
    {
      selector: 'edge.highlighted-path',
      style: {
        'line-color': C.highlight,
        width: 2.8,
        opacity: 0.94,
      },
    },

    // ═══ Labels ═══════════════════════════════════════════════════════════
    {
      selector: 'node',
      style: {
        label: 'data(labelText)',
        color: '#1e293b',
        'font-size': 10,
        'font-weight': 'bold',
        'text-valign': 'bottom',
        'text-halign': 'center',
        'text-margin-y': 6,
        'text-wrap': 'wrap',
        'text-max-width': '120px',
        'font-family': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        'text-background-color': 'rgba(255,255,255,0.78)',
        'text-background-opacity': 0.95,
        'text-background-padding': '3px 7px',
        'text-background-shape': 'round-rectangle',
      },
    },
    {
      selector: 'node[type="agent"]',
      style: { label: '' },
    },

    // ═══ Edge labels ══════════════════════════════════════════════════════
    {
      selector: 'edge',
      style: {
        label: 'data(edgeLabel)',
        color: '#64748b',
        'font-size': 8,
        'font-weight': '600',
        'font-family': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        'text-background-color': 'rgba(255,255,255,0.82)',
        'text-background-opacity': 0.88,
        'text-background-padding': '2px 5px',
        'text-background-shape': 'round-rectangle',
      },
    },
  ]
}

// ── Data mappers ──────────────────────────────────────────────────────────────

export function nodeToElement(node) {
  return {
    group: 'nodes',
    data: {
      id: node.id,
      labelText: node.name || node.id,
      type: node.type || 'switch',
      state: node.state || 'normal',
      role: node.role || '',
      detail: node.detail || '',
      kpi: node.kpi || '-',
      ports: node.ports || [],
      rawId: node.rawId || '',
      interface: node.interface || '',
      origX: node.x,
      origY: node.y,
    },
    position: node.x != null && node.y != null
      ? { x: node.x * 10, y: node.y * 10 }
      : undefined,
  }
}

export function linkToElement(link) {
  const lp = []
  if (link.role === 'backup') lp.push('备')
  if (link.bandwidth_mbps) lp.push(link.bandwidth_mbps + 'M')
  return {
    group: 'edges',
    data: {
      id: link.id,
      source: link.from,
      target: link.to,
      tone: link.tone || 'primary',
      state: link.state || 'normal',
      role: link.role || 'primary',
      cost: link.cost ?? 1,
      bandwidth: link.bandwidth_mbps || null,
      delay: link.delay_ms || null,
      fromPort: link.fromPort ?? null,
      toPort: link.toPort ?? null,
      pathRole: link.pathRole || null,
      edgeLabel: lp.join(' '),
    },
  }
}

export function elementToStoreLink(edge) {
  const d = edge.data()
  return {
    id: d.id, from: d.source, to: d.target,
    tone: d.tone, state: d.state, role: d.role,
    cost: d.cost, bandwidth_mbps: d.bandwidth,
  }
}

export const palette = C
