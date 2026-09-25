import { useEffect, useState } from 'react'
import { Check, Copy } from 'lucide-react'

export interface CodeSampleProps {
  title: string
  code: string
  tone: 'vulnerable' | 'secure'
  copyLabel: string
  copiedLabel: string
}

/**
 * Bloque de código del catálogo de remediación. Se usa el mismo visor que en la
 * ficha de vulnerabilidad para que el ejemplo vulnerable y el seguro se lean
 * igual que la evidencia real del escaneo.
 */
export function CodeSample({ title, code, tone, copyLabel, copiedLabel }: CodeSampleProps) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) {
      return
    }
    const timer = window.setTimeout(() => setCopied(false), 2000)
    return () => window.clearTimeout(timer)
  }, [copied])

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className={tone === 'vulnerable' ? 'code-panel code-vulnerable' : 'code-panel code-secure'}>
      <div className="code-panel-header">
        <p className="code-panel-title">{title}</p>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void handleCopy()}
          aria-label={`${copied ? copiedLabel : copyLabel}: ${title}`}
        >
          {copied ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
          <span>{copied ? copiedLabel : copyLabel}</span>
        </button>
      </div>
      <pre className="code-block" tabIndex={0} aria-label={title}>
        <code>{code}</code>
      </pre>
    </div>
  )
}
