import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Assistant text. Links open in a new tab, since they are citations. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="prose-kairos">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
