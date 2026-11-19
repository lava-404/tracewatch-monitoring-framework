import React, { useMemo, useState } from 'react'
import PageHeader from '../components/PageHeader'
import { api } from '../hooks/useApi'
import {
  Activity, ArrowRight, CheckCircle2, CreditCard, AlertTriangle,
  Workflow, ShieldCheck, X, Clock, Layers, Server, Zap,
  Play, RefreshCw, ExternalLink,
} from 'lucide-react'

const scenarios = [
  {
    id: 'payment',
    title: 'Complete Payment Flow',
    description: 'Run a real order through the API Gateway, Order, Payment, and Notification services.',
    icon: CreditCard,
    failurePayload: {},
  },
  {
    id: 'failure',
    title: 'Failure Execution',
    description: 'Inject a payment failure and inspect how it propagates through the system.',
    icon: AlertTriangle,
    failurePayload: { simulate_payment_failure: true },
  },
]

const stages = [
  { id: 'api-gateway', name: 'API Gateway', description: 'Receives and routes the incoming request.', icon: Server },
  { id: 'order-service', name: 'Order Service', description: 'Validates and orchestrates order creation.', icon: Layers },
  { id: 'payment-service', name: 'Payment Service', description: 'Processes the payment transaction.', icon: CreditCard },
  { id: 'notification-service', name: 'Notification Service', description: 'Sends the order confirmation notification.', icon: Zap },
]

const tagMap = (span) => Object.fromEntries((span.tags || []).map((tag) => [tag.key, tag.value]))
const ms = (durationUs) => `${(Number(durationUs || 0) / 1000).toFixed(2)} ms`
const normalized = (value = '') => String(value).toLowerCase().replace(/[^a-z0-9]/g, '')

function serviceNameForSpan(trace, span) {
  return trace?.processes?.[span.processID]?.serviceName || span.serviceName || 'unknown-service'
}

function serviceSpans(trace, serviceName) {
  const spans = trace?.spans || []
  const target = normalized(serviceName)
  return spans.filter((span) => {
    const actual = normalized(serviceNameForSpan(trace, span))
    return actual === target || actual.includes(target) || target.includes(actual)
  })
}

function getStageData(trace, stage) {
  const spans = serviceSpans(trace, stage.id)
  // Prefer server spans, then meaningful application spans, then any span at all.
  const serverSpans = spans.filter((span) => normalized(tagMap(span)['span.kind']) === 'server')
  const applicationSpans = spans.filter((span) => !/http\s+(send|receive)|\.send$|\.receive$/i.test(span.operationName || ''))
  const candidates = serverSpans.length ? serverSpans : applicationSpans.length ? applicationSpans : spans
  const latencySpan = candidates.reduce((longest, span) => !longest || Number(span.duration || 0) > Number(longest.duration || 0) ? span : longest, null)
  const failed = spans.some((span) => {
    const tags = tagMap(span)
    return tags.error === true || String(tags.error).toLowerCase() === 'true' || Number(tags['http.status_code'] ?? tags['http.status_code']) >= 400 || tags['payment.status'] === 'failed' || tags['notification.status'] === 'failed'
  })
  const status = spans.length === 0 ? 'NOT SEEN' : failed ? 'FAILED' : 'SUCCESS'
  return {
    ...stage,
    spans,
    status,
    latency: latencySpan ? ms(latencySpan.duration) : '—',
    latencyRaw: Number(latencySpan?.duration || 0),
    processed: spans.length && !failed ? 1 : 0,
    failed: failed ? 1 : 0,
    health: spans.length ? (failed ? 'Degraded' : 'Healthy') : 'Not observed',
    latencySpan,
    error: spans.find((span) => {
      const tags = tagMap(span)
      return tags.error || Number(tags['http.status_code']) >= 400
    }) || null,
  }
}

function StatusPill({ status }) {
  const styles = {
    SUCCESS: 'bg-emerald-500/10 text-emerald-500',
    FAILED: 'bg-rose-500/10 text-rose-500',
    RUNNING: 'bg-amber-500/10 text-amber-500',
    'NOT SEEN': 'bg-gray-500/10 text-text-muted',
  }
  const dot = { SUCCESS: 'bg-emerald-500', FAILED: 'bg-rose-500', RUNNING: 'bg-amber-500', 'NOT SEEN': 'bg-gray-400' }
  return (
    <span className={`flex items-center gap-2 text-xs px-2.5 py-1 rounded-full ${styles[status] || styles['NOT SEEN']}`}>
      <span className={`w-2 h-2 rounded-full ${dot[status] || dot['NOT SEEN']}`} />
      {status}
    </span>
  )
}

function Metric({ label, value }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-text-muted mb-1">{label}</p>
      <p className="text-xl font-semibold text-text-primary">{value ?? '—'}</p>
    </div>
  )
}

function StageCard({ stage, onClick }) {
  const Icon = stage.icon
  return (
    <button onClick={onClick} disabled={!stage.spans?.length} className="group flex-shrink-0 w-[290px] text-left bg-bg-card border border-bg-border rounded-2xl p-5 hover:border-accent-primary/40 hover:shadow-lg transition-all disabled:opacity-60 disabled:cursor-default">
      <div className="flex items-center justify-between mb-5">
        <div className="p-3 rounded-xl bg-bg-secondary text-text-secondary"><Icon size={21} /></div>
        <StatusPill status={stage.status || 'NOT SEEN'} />
      </div>
      <h3 className="text-base font-semibold text-text-primary">{stage.name}</h3>
      <p className="text-sm text-text-secondary mt-1">{stage.latency} latency</p>
      <div className="border-t border-bg-border my-5" />
      <div className="grid grid-cols-2 gap-y-5 gap-x-4">
        <Metric label="Spans" value={stage.spans?.length || 0} />
        <Metric label="Processed" value={stage.processed ?? '—'} />
        <Metric label="Failed" value={stage.failed ?? '—'} />
        <Metric label="Health" value={stage.health || '—'} />
      </div>
      <div className="mt-5 pt-4 border-t border-bg-border flex items-center justify-between">
        <span className="text-xs text-text-muted">{stage.id}</span>
        <span className="text-xs text-accent-primary opacity-0 group-hover:opacity-100">View trace →</span>
      </div>
    </button>
  )
}

function TraceTimeline({ trace, selectedService }) {
  const spans = trace?.spans || []
  if (!spans.length) return null
  const start = Math.min(...spans.map((span) => Number(span.startTime || 0)))
  const end = Math.max(...spans.map((span) => Number(span.startTime || 0) + Number(span.duration || 0)))
  const total = Math.max(end - start, 1)
  const sorted = [...spans].sort((a, b) => Number(a.startTime || 0) - Number(b.startTime || 0))
  return (
    <div className="border border-bg-border rounded-xl overflow-hidden">
      <div className="px-4 py-3 bg-bg-secondary flex flex-wrap justify-between gap-2 text-xs text-text-secondary">
        <span>Trace timeline · {spans.length} spans · {ms(total)}</span>
        <span>Selected service highlighted</span>
      </div>
      <div className="max-h-[360px] overflow-auto p-3 space-y-2">
        {sorted.map((span) => {
          const service = serviceNameForSpan(trace, span)
          const isSelected = normalized(service) === normalized(selectedService)
          const left = Math.max(0, ((Number(span.startTime || 0) - start) / total) * 100)
          const width = Math.max(0.7, (Number(span.duration || 0) / total) * 100)
          const tags = tagMap(span)
          const isError = tags.error === true || String(tags.error).toLowerCase() === 'true' || Number(tags['http.status_code']) >= 400
          return (
            <div key={span.spanID} className={`grid grid-cols-[minmax(150px,240px)_1fr_90px] gap-3 items-center text-xs ${isSelected ? 'text-text-primary' : 'text-text-muted'}`} title={`${service} · ${span.operationName} · ${ms(span.duration)}`}>
              <div className="truncate"><div className="font-medium truncate">{span.operationName}</div><div className="opacity-70 truncate">{service}</div></div>
              <div className="relative h-5 bg-bg-secondary rounded overflow-hidden">
                <div className={`absolute top-1 bottom-1 rounded ${isError ? 'bg-rose-500' : isSelected ? 'bg-accent-primary' : 'bg-indigo-500/50'}`} style={{ left: `${left}%`, width: `${width}%` }} />
              </div>
              <div className="text-right whitespace-nowrap">{ms(span.duration)}</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function StageDetailsModal({ stage, trace, traceId, onClose }) {
  const Icon = stage.icon
  const [showAllSpans, setShowAllSpans] = useState(false)
  const visibleSpans = showAllSpans ? (trace?.spans || []) : stage.spans
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onClick={onClose}>
      <div className="bg-bg-card border border-bg-border rounded-3xl w-full max-w-6xl max-h-[92vh] overflow-y-auto shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between p-6 border-b border-bg-border">
          <div>
            <div className="flex items-center gap-3 flex-wrap"><Icon size={22} /><h2 className="text-xl font-semibold text-text-primary">{stage.name}</h2><StatusPill status={stage.status} /></div>
            <p className="text-sm text-text-secondary mt-2">{stage.description}</p>
            <p className="text-xs text-text-muted mt-2">Service: {stage.id} · Trace ID: {traceId}</p>
          </div>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-bg-secondary"><X size={20} /></button>
        </div>
        <div className="p-6 space-y-6">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Metric label="Latency" value={stage.latency} />
            <Metric label="Spans" value={stage.spans.length} />
            <Metric label="Processed" value={stage.processed} />
            <Metric label="Failed" value={stage.failed} />
          </div>
          <div>
            <h3 className="font-semibold text-text-primary mb-3">Distributed trace timeline</h3>
            <TraceTimeline trace={trace} selectedService={stage.id} />
          </div>
          <div>
            <div className="flex items-center justify-between gap-3 mb-3">
              <h3 className="font-semibold text-text-primary">Span details {showAllSpans ? '(all services)' : `(${stage.name})`}</h3>
              <button onClick={() => setShowAllSpans((value) => !value)} className="text-xs text-accent-primary hover:underline">{showAllSpans ? 'Show selected service only' : 'Show all trace spans'}</button>
            </div>
            <div className="overflow-x-auto border border-bg-border rounded-xl">
              <table className="min-w-full text-sm">
                <thead className="bg-bg-secondary text-text-secondary"><tr><th className="text-left p-3">Operation</th><th className="text-left p-3">Service</th><th className="text-left p-3">Duration</th><th className="text-left p-3">HTTP Status</th><th className="text-left p-3">Span ID</th></tr></thead>
                <tbody>
                  {visibleSpans.map((span) => {
                    const tags = tagMap(span)
                    return <tr key={span.spanID} className="border-t border-bg-border"><td className="p-3 text-text-primary font-medium">{span.operationName}</td><td className="p-3 text-text-secondary">{serviceNameForSpan(trace, span)}</td><td className="p-3 whitespace-nowrap">{ms(span.duration)}</td><td className="p-3">{tags['http.status_code'] ?? tags['http.status'] ?? '—'}</td><td className="p-3 font-mono text-xs text-text-muted">{span.spanID}</td></tr>
                  })}
                </tbody>
              </table>
            </div>
          </div>
          {stage.error && <div className="p-4 rounded-xl bg-rose-500/10 text-rose-500"><strong>Error span:</strong> {stage.error.operationName}</div>}
        </div>
      </div>
    </div>
  )
}

export default function LiveExecution() {
  const [selectedScenario, setSelectedScenario] = useState('payment')
  const [selectedStage, setSelectedStage] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [trace, setTrace] = useState(null)
  const [traceId, setTraceId] = useState('')
  const [executionResult, setExecutionResult] = useState(null)
  const [runTime, setRunTime] = useState(null)
  const [waitingForTrace, setWaitingForTrace] = useState(false)
  const scenario = scenarios.find((item) => item.id === selectedScenario) || scenarios[0]
  const ScenarioIcon = scenario.icon
  const stageData = useMemo(() => stages.map((stage) => getStageData(trace, stage)), [trace])
  const totalDuration = trace?.spans?.reduce((max, span) => Math.max(max, Number(span.duration || 0)), 0) || 0
  const jaegerUrl = import.meta.env.VITE_JAEGER_URL || "http://localhost:16686";
  const traceUrl = `${jaegerUrl}/trace/${traceId}`;
  async function runScenario() {
    setLoading(true)
    setError('')
    setTrace(null)
    setTraceId('')
    setExecutionResult(null)
    setSelectedStage(null)
  
    try {
      const payload = {
        scenario: scenario.id === 'failure'
          ? 'failure-execution'
          : 'complete-payment-flow',
        ...scenario.failurePayload,
      }
  
      const response = await api.post('/api/simulate/order-flow', payload)
      const result = response.data
  
      if (!result) {
        throw new Error('No response received from API Gateway')
      }
  
      const id = result.trace_id
  
      setExecutionResult(result)
      setTraceId(id || '')
      setRunTime(new Date())
  
      // Execution succeeds independently of Jaeger availability.
      if (id) {
        try {
          const traceResponse = await api.get(
            `/api/observability/traces/${id}`
          )
  
          const traceData = traceResponse.data
          const foundTrace = traceData?.data?.[0] || null
  
          if (foundTrace?.spans?.length) {
            setTrace(foundTrace)
          }
        } catch (traceError) {
          console.warn('Trace unavailable:', traceError)
        }
      }
    } catch (err) {
      setError(err.message || 'Execution failed')
    } finally {
      setLoading(false)
      setWaitingForTrace(false)
    }
  }

  return (
    <div className="p-6 max-w-[1600px] mx-auto">
      <PageHeader title="Live Execution" subtitle="Trigger real service executions and inspect the distributed trace collected by OpenTelemetry and Jaeger." />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
        {scenarios.map((item) => {
          const Icon = item.icon
          const active = selectedScenario === item.id
          return <button key={item.id} onClick={() => { setSelectedScenario(item.id); setTrace(null); setTraceId(''); setExecutionResult(null); setError('') }} className={`text-left rounded-2xl border p-5 transition-all ${active ? 'border-accent-primary bg-accent-primary/5' : 'border-bg-border bg-bg-card hover:border-accent-primary/30'}`}><div className="flex items-start gap-4"><div className={`p-3 rounded-xl ${active ? 'bg-accent-primary/10 text-accent-primary' : 'bg-bg-secondary text-text-secondary'}`}><Icon size={22} /></div><div className="flex-1"><h3 className="font-semibold text-text-primary">{item.title}</h3><p className="text-sm text-text-secondary mt-2">{item.description}</p></div>{active && <CheckCircle2 size={18} className="text-accent-primary" />}</div></button>
        })}
      </div>
      <section className="bg-bg-card border border-bg-border rounded-2xl p-5 mb-6"><div className="flex flex-col md:flex-row md:items-center justify-between gap-4"><div className="flex items-center gap-3"><div className="p-3 rounded-xl bg-bg-secondary"><ScenarioIcon size={22} /></div><div><h2 className="font-semibold text-text-primary">{scenario.title}</h2><p className="text-sm text-text-secondary mt-1">{trace
  ? 'Execution completed · trace loaded'
  : waitingForTrace
    ? 'Execution succeeded · waiting for Jaeger to index trace…'
    : 'Ready to execute against your running services'}</p></div></div><button onClick={runScenario} disabled={loading} className="inline-flex items-center justify-center gap-2 px-5 py-3 rounded-xl bg-accent-primary text-white font-medium disabled:opacity-60">{loading ? <><RefreshCw size={17} className="animate-spin" /> Running…</> : <><Play size={17} /> Run Execution</>}</button></div></section>
      {error && <div className="mb-6 p-4 rounded-xl border border-rose-500/30 bg-rose-500/10 text-rose-500 text-sm">{error}</div>}
      <section className="bg-bg-card border border-bg-border rounded-3xl p-6 md:p-8"><div className="flex items-start gap-4 mb-6"><div className="p-3 rounded-xl bg-bg-secondary"><Workflow size={22} /></div><div><h2 className="text-xl font-semibold text-text-primary">Execution Flow</h2><p className="text-sm text-text-secondary mt-1">Stages are populated from spans returned by Jaeger for this execution.</p></div></div><div className="overflow-x-auto pb-4"><div className="flex items-stretch min-w-max">{stageData.map((stage, index) => <React.Fragment key={stage.id}><StageCard stage={stage} onClick={() => setSelectedStage(stage)} />{index < stageData.length - 1 && <div className="flex items-center justify-center w-14 flex-shrink-0"><ArrowRight size={20} className="text-text-muted" /></div>}</React.Fragment>)}</div></div><div className="border-t border-bg-border pt-4 mt-3 flex flex-wrap items-center gap-4 text-xs text-text-muted"><span className="flex items-center gap-2"><span className="w-2 h-2 rounded-full bg-emerald-500" />Success</span><span className="flex items-center gap-2"><span className="w-2 h-2 rounded-full bg-rose-500" />Failed</span><span className="flex items-center gap-2"><span className="w-2 h-2 rounded-full bg-gray-400" />Not observed</span><span>Duration values are sourced from Jaeger spans.</span></div></section>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6"><section className="bg-bg-card border border-bg-border rounded-2xl p-6"><div className="flex items-center gap-3 mb-4"><ShieldCheck size={20} /><h3 className="font-semibold text-text-primary">Execution Summary</h3></div><div className="space-y-3 text-sm">{[['Scenario', scenario.title],['Execution status', trace ? 'Trace loaded' : loading ? 'Running' : 'Not started'],['Observed services', trace ? new Set(Object.values(trace.processes || {}).map((p) => p.serviceName)).size : '—'],['Total trace duration', trace ? ms(totalDuration) : '—'],['Order ID', executionResult?.results?.order?.order_id || '—'],['Trace ID', traceId || '—']].map(([label, value]) => <div key={label} className="flex justify-between gap-3"><span className="text-text-secondary">{label}</span><span className="text-text-primary font-medium break-all text-right">{value}</span></div>)}{traceId && <a className="inline-flex items-center gap-2 text-accent-primary mt-2" href={traceUrl} target="_blank" rel="noreferrer">Open trace in Jaeger <ExternalLink size={14} /></a>}</div></section><section className="bg-bg-card border border-bg-border rounded-2xl p-6"><div className="flex items-center gap-3 mb-4"><Clock size={20} /><h3 className="font-semibold text-text-primary">Trace Details</h3></div>{trace ? <div className="space-y-2 text-sm text-text-secondary"><p><strong className="text-text-primary">Spans:</strong> {trace.spans.length}</p><p><strong className="text-text-primary">Services:</strong> {[...new Set(Object.values(trace.processes || {}).map((p) => p.serviceName))].join(', ')}</p><p><strong className="text-text-primary">Executed:</strong> {runTime?.toLocaleString()}</p><p className="text-xs break-all pt-2">Trace ID: {traceId}</p></div> : <p className="text-sm text-text-secondary">Run a scenario to load the actual distributed trace, span hierarchy, service timing, and error information.</p>}</section></div>
      {selectedStage && <StageDetailsModal stage={selectedStage} trace={trace} traceId={traceId} onClose={() => setSelectedStage(null)} />}
    </div>
  )
}
