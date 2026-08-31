'use client';
import { useState } from 'react';
import dynamic from 'next/dynamic';
import type { DecisionGraphType } from '@gorules/jdm-editor';

// Dynamically import the editor, completely disabling Server-Side Rendering
const JdmEditorWrapper = dynamic(() => import('../components/JdmEditorClient'), { 
  ssr: false,
  loading: () => <div className="flex items-center justify-center h-full">Loading Editor Canvas...</div>
});

export default function Home() {
  const [requirements, setRequirements] = useState("");
  const [loading, setLoading] = useState(false);
  
  const [graph, setGraph] = useState<DecisionGraphType>({ 
    nodes: [], 
    edges: [] 
  });

  const handleGenerate = async () => {
    if (!requirements.trim()) return;
    
    setLoading(true);
    try {
      const res = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requirements })
      });
      
      if (res.ok) {
        const data = await res.json();
        setGraph(data.jdm);
      } else {
        console.error("Failed to generate graph");
      }
    } catch (err) {
      console.error("API error:", err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col h-screen bg-gray-50 p-4 dark:bg-zinc-900">
      <div className="mb-4 flex gap-4">
        <input 
          type="text" 
          className="flex-1 p-3 border rounded shadow-sm text-black"
          value={requirements} 
          onChange={(e) => setRequirements(e.target.value)} 
          placeholder="Describe the decision logic (e.g., 'Create a pricing tier rule based on customer age...')"
          onKeyDown={(e) => e.key === 'Enter' && handleGenerate()}
        />
        <button 
          className="bg-blue-600 text-white px-6 py-3 rounded shadow hover:bg-blue-700 disabled:opacity-50"
          onClick={handleGenerate}
          disabled={loading}
        >
          {loading ? 'Generating...' : 'Generate Graph'}
        </button>
      </div>
      
      <div className="flex-1 border rounded shadow-sm bg-white overflow-hidden text-black relative">
        {/* Render the dynamically imported component here */}
        <JdmEditorWrapper value={graph} onChange={setGraph} />
      </div>
    </div>
  );
}