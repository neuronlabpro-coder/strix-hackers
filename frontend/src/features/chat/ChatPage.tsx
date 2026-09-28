import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { ChatContextOptions } from '../../types/api'
import { ChatComposer } from './ChatComposer'
import { ConversationSidebar } from './ConversationSidebar'
import { ChatThread } from './ChatThread'
import { ChatWelcome } from './ChatWelcome'
import { useChat } from './useChat'

/**
 * La vista de `/chat`: el menu de hilos a la izquierda y la conversacion a la derecha.
 *
 * ## Por que el compositor esta **siempre** visible, incluso sin hilo
 *
 * Porque un chat al que hay que crearle un hilo antes de poder escribir es un chat con un paso
 * de mas, y ese paso se olvida. El hilo se crea en cuanto hay algo que enviar, que es el
 * unico momento en que de verdad existe una conversacion.
 *
 * ## Por que el borrador sobrevive al cambio de hilo
 *
 * Porque el usuario puede empezar a escribir, acordarse de algo en otro hilo, ir a mirarlo y
 * volver. Perder el texto por cambiar de conversacion no es un detalle: es trabajo suyo
 * evaporado sin aviso, y el usuario no tiene forma de recuperarlo. El borrador es uno solo, no
 * uno por hilo, porque un borrador por hilo obligaria a recordar en cual estaba escrito.
 */

export function ChatPage() {
  const { t } = useTranslation('chat')
  const chat = useChat()
  const [borrador, setBorrador] = useState('')
  /**
   * El texto que se reenvia al pulsar "Reintentar", **con el hilo al que pertenece**.
   *
   * Lleva el identificador y no solo el texto para poder derivar si el fallo sigue siendo
   * relevante. Un fallo de envio pertenece al hilo en el que fallo, y arrastrarlo al siguiente
   * haria ver un error en una conversacion que no ha tenido ninguno.
   *
   * La alternativa —un `useEffect` que lo limpia al cambiar de hilo— seria un `setState` desde
   * un efecto, que arranca un render que el anterior ya habia terminado. Guardando el hilo
   * junto al texto, la pregunta "este fallo es de aqui" se responde **durante** el render y no
   * hace falta ningun efecto. El borrador, en cambio, **si** sobrevive al cambio de hilo a
   * proposito: perder el texto por mirar otro hilo es trabajo del usuario evaporado sin aviso.
   */
  const [ultimoIntentado, setUltimoIntento] = useState<{
    texto: string
    conversationId: string | null
  } | null>(null)
  const [ultimasOpciones, setUltimasOpciones] = useState<ChatContextOptions | undefined>()

  const falloDeEsteHilo = ultimoIntentado?.conversationId === chat.activeId
  const textoParaReintentar = falloDeEsteHilo ? ultimoIntentado.texto : null

  async function enviar(options?: ChatContextOptions): Promise<void> {
    const texto = borrador.trim()
    if (!texto) return
    // El hilo se guarda **antes** de la llamada, y con el valor de `activeId` de este momento.
    // Si se guardara despues, y `send` crease el hilo al vuelo, el identificador guardado
    // seria `null` y el fallo nunca se reconoceria como de este hilo: el boton de reintentar
    // desapareceria justo cuando hace falta.
    setUltimoIntento({ texto, conversationId: chat.activeId })
    setUltimasOpciones(options)
    const ok = await chat.send(texto, options)
    if (ok) {
      setBorrador('')
      setUltimoIntento(null)
    }
  }

  async function reintentar(): Promise<void> {
    if (textoParaReintentar === null) return
    const ok = await chat.send(textoParaReintentar, ultimasOpciones)
    if (ok) {
      setUltimoIntento(null)
    }
  }

  function tomarPrompt(prompt: string): void {
    // El prompt de una tarjeta o un chip **se anade** al final de lo que ya haya escrito y no
    // lo sustituye. Sustituirlo seria perder lo que el usuario llevaba medio minuto escribiendo
    // por haber pulsado una tarjeta sin querer mirar lo que hacia.
    setBorrador((actual) => (actual.trim() ? `${actual.trim()} ${prompt}` : prompt))
  }

  const hayHilo = chat.activeId !== null

  return (
    <section className="page-section chat-page" aria-labelledby="chat-title">
      <h1 id="chat-title" className="visually-hidden">
        {t('title')}
      </h1>

      <div className="chat-layout">
        <ConversationSidebar
          conversations={chat.conversations}
          activeId={chat.activeId}
          isLoading={chat.isLoadingList}
          loadFailed={chat.listFailed}
          total={chat.total}
          onSelect={chat.select}
          onNew={() => void chat.startNew()}
          onRemove={(id) => void chat.remove(id)}
          onRetry={chat.retryList}
        />

        <div className="chat-main">
          {!hayHilo && !chat.isLoadingThread && <ChatWelcome onPick={tomarPrompt} />}

          {hayHilo && chat.isLoadingThread && <p className="chat-thread-loading">{t('thread.loading')}</p>}

          {hayHilo && !chat.isLoadingThread && (
            <ChatThread messages={chat.messages} sources={chat.lastSources} isSending={chat.isSending} />
          )}

          <ChatComposer
            isSending={chat.isSending}
            sendFailed={chat.sendFailed && textoParaReintentar !== null}
            value={borrador}
            onValueChange={setBorrador}
            onSend={(options) => void enviar(options)}
            onRetry={() => void reintentar()}
          />
        </div>
      </div>
    </section>
  )
}
