import { useEffect, useRef } from 'react'
import { AlertTriangle, FileText, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ChatContextSource, ChatMessage } from '../../types/api'
import { Markdown } from './Markdown'

/**
 * El hilo de una conversacion: los mensajes del usuario, los del asistente y lo que se ha
 * inyectado de contexto.
 *
 * ## Por que el mensaje del usuario sale en crudo y el del asistente en Markdown
 *
 * Porque son dos cosas distintas y usar el mismo renderizador en las dos seria un error en una
 * de las dos direcciones. El usuario escribe Markdown a proposito —quiere una lista, quiere
 * codigo— y si se lo interpretas le has cambiado lo que escribio sin avisar. El asistente, en
 * cambio, devuelve Markdown porque es un modelo de lenguaje, y renderizarlo como texto plano
 * haria que sus listas salieran como `1. **algo**` en una sola linea.
 *
 * ## Por que el scroll se ancla al final y no a la posicion
 *
 * Porque el hilo crece hacia abajo, y el sitio donde el usuario ha estado leyendo es el final.
 * Cuando llega una respuesta larga mientras el usuario esta leyendo mas arriba, el scroll se
 * le escapa de las manos y no puede volver a el sin buscarlo. El anclaje se hace solo cuando
 * el usuario **ya estaba** cerca del final —no siempre— y el indicador de escritura avisa de
 * que ha llegado algo, para que el scroll automatico no le robe nada.
 */

export interface ChatThreadProps {
  messages: ChatMessage[]
  sources: ChatContextSource[]
  isSending: boolean
}

export function ChatThread({ messages, sources, isSending }: ChatThreadProps) {
  const { t } = useTranslation('chat')
  const finRef = useRef<HTMLDivElement | null>(null)
  const contenedorRef = useRef<HTMLDivElement | null>(null)
  /** Si el usuario esta leyendo arriba, para no arrastrarle al final sin avisar. */
  const pegadoAlFinal = useRef(true)

  useEffect(() => {
    // Se mide antes de pintar, porque despues el DOM ya ha crecido y la distancia al final
    // seria cero en cualquier caso: medir despues siempre dira "estabas al final".
    const contenedor = contenedorRef.current
    if (contenedor) {
      const distanciaAlFinal = contenedor.scrollHeight - contenedor.scrollTop - contenedor.clientHeight
      pegadoAlFinal.current = distanciaAlFinal < 120
    }
    if (pegadoAlFinal.current && finRef.current) {
      finRef.current.scrollIntoView({ block: 'end' })
    }
  }, [isSending, messages])

  return (
    <div className="chat-thread" ref={contenedorRef}>
      {messages.length === 0 && !isSending && (
        <p className="chat-thread-empty">{t('thread.empty')}</p>
      )}

      {messages.map((mensaje) => (
        <article
          className={
            mensaje.role === 'user' ? 'chat-bubble chat-bubble-user' : 'chat-bubble chat-bubble-assistant'
          }
          key={mensaje.id}
        >
          {mensaje.role === 'assistant' ? (
            <>
              <Markdown
                content={mensaje.content}
                copyLabel={t('thread.copy')}
                copiedLabel={t('thread.copied')}
                codeLabel={t('thread.code')}
              />
              <footer className="chat-bubble-foot">
                {mensaje.consumo_no_verificable ? (
                  // El consumo no verificado **no** se muestra como un cero: son dos cosas
                  // distintas y confundirlas haria que un fallo de medicion del proveedor se
                  // leyera como un ahorro. El aviso lo dice en claro.
                  <span className="chat-cost chat-cost-unverified" title={t('thread.unverifiedHelp')}>
                    <AlertTriangle size={12} aria-hidden="true" />
                    {t('thread.unverified')}
                  </span>
                ) : (
                  <span className="chat-cost">
                    {t('thread.cost', {
                      count: Number(mensaje.credits_cost ?? 0).toFixed(4),
                    })}
                  </span>
                )}
                {mensaje.model_id && (
                  <span className="chat-model" title={mensaje.model_id}>
                    {mensaje.model_id}
                  </span>
                )}
              </footer>
            </>
          ) : (
            // El texto del usuario va en un `<pre>` en vez de en un `div`, no por formato sino
            // por seguridad: un `<pre>` respeta los saltos de linea, y un `white-space` de CSS
            // tambien, pero el `<pre>` no necesita que la hoja de estilos haya cargado para no
            // mostrar una sola linea larga durante el primer instante.
            <div className="chat-bubble-text">{mensaje.content}</div>
          )}
        </article>
      ))}

      {isSending && (
        <div className="chat-bubble chat-bubble-assistant">
          <div className="chat-typing" role="status" aria-label={t('thread.thinking')}>
            <Loader2 size={15} className="chat-typing-spinner" aria-hidden="true" />
            <span>{t('thread.thinking')}</span>
          </div>
        </div>
      )}

      {sources.length > 0 && (
        <div className="chat-sources">
          <p className="chat-sources-title">
            <FileText size={13} aria-hidden="true" />
            {t('sources.title', { count: sources.length })}
          </p>
          <ul className="chat-sources-list">
            {sources.map((documento) => (
              <li key={documento.id}>
                <span className="chat-sources-name">{documento.title}</span>
                <span className="chat-sources-description">{documento.description}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div ref={finRef} />
    </div>
  )
}
