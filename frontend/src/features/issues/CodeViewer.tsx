import { useState } from 'react'
import { Check, Copy } from 'lucide-react'

export interface CodeViewerProps {
  title: string
  caption?: string
  code: string
  /** Etiqueta accesible del botón de copiado. */
  copyLabel: string
  copiedLabel: string
}

/**
 * Visor de código inmutable. R4 impide la edición: el bloque solo se copia, y el
 * texto se expone en `whitespace: pre` para conservar el formato de la prueba.
 */
export function CodeViewer({ title, caption, code, copyLabel, copiedLabel }: CodeViewerProps) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="code-panel">
      <div className="code-panel-header">
        <div>
          <p className="code-panel-title">{title}</p>
          {caption ? <p className="chart-empty">{caption}</p> : null}
        </div>
        <button className="secondary-button" type="button" onClick={() => void handleCopy()}>
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
