"""
RAG Service for direct data retrieval bypassing Azure OpenAI On Your Data.

This service enables direct querying of data sources for reasoning models
that are incompatible with the OpenAI SDK's On Your Data feature.
"""

import asyncio
import json
import logging
import base64
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple


class RAGDocument:
    """Represents a retrieved document with content and metadata."""
    
    def __init__(self, content: str, title: str = None, url: str = None, 
                 filename: str = None, score: float = None, **metadata):
        self.content = content
        self.title = title
        self.url = url
        self.filename = filename
        self.score = score
        self.metadata = metadata
    
    def to_citation_dict(self) -> Dict[str, Any]:
        """Convert to citation format for frontend compatibility."""
        citation = {
            "content": self.content,
            "title": self.title or "Document",
            "url": self.url,
            "filepath": self.filename,
            "chunk_id": self.metadata.get("chunk_id", "0")
        }
        return {k: v for k, v in citation.items() if v is not None}


class BaseRAGRetriever(ABC):
    """Base class for RAG document retrievers."""
    
    @abstractmethod
    async def retrieve(self, query: str, top_k: int = 5) -> List[RAGDocument]:
        """Retrieve relevant documents for the given query."""
        pass
    
    @abstractmethod
    async def close(self):
        """Clean up resources."""
        pass


class AzureSearchRAGRetriever(BaseRAGRetriever):
    """RAG retriever for Azure Cognitive Search."""
    
    def __init__(self, settings):
        self.settings = settings
        self.search_client = None
        self.embedding_client = None
        self._initialized = False
    
    async def _initialize(self):
        """Initialize the search client and embedding client if needed."""
        if self._initialized:
            return
            
        try:
            # Initialize search client
            from azure.identity.aio import DefaultAzureCredential
            from azure.search.documents.aio import SearchClient
            from azure.search.documents.models import VectorizedQuery
            from openai import AsyncAzureOpenAI
            
            if self.settings.datasource.key:
                from azure.core.credentials import AzureKeyCredential
                credential = AzureKeyCredential(self.settings.datasource.key)
            else:
                credential = DefaultAzureCredential()
            
            self.search_client = SearchClient(
                endpoint=self.settings.datasource.endpoint,
                index_name=self.settings.datasource.index,
                credential=credential
            )
            
            # Initialize embedding client if needed for vector search
            if self.settings.datasource.query_type in ["vector", "vector_simple_hybrid", "vector_semantic_hybrid"]:
                embedding_dependency = self.settings.azure_openai.extract_embedding_dependency()
                if embedding_dependency and embedding_dependency.get("type") == "deployment_name":
                    # Use same Azure OpenAI client for embeddings
                    azure_openai_client = None
                    try:
                        if self.settings.azure_openai.key:
                            azure_openai_client = AsyncAzureOpenAI(
                                api_version=self.settings.azure_openai.preview_api_version,
                                api_key=self.settings.azure_openai.key,
                                azure_endpoint=self.settings.azure_openai.endpoint,
                            )
                        else:
                            from azure.identity.aio import get_bearer_token_provider
                            async with DefaultAzureCredential() as credential:
                                ad_token_provider = get_bearer_token_provider(
                                    credential,
                                    "https://cognitiveservices.azure.com/.default"
                                )
                            azure_openai_client = AsyncAzureOpenAI(
                                api_version=self.settings.azure_openai.preview_api_version,
                                azure_ad_token_provider=ad_token_provider,
                                azure_endpoint=self.settings.azure_openai.endpoint,
                            )
                        self.embedding_client = azure_openai_client
                    except Exception as e:
                        logging.warning(f"Failed to initialize embedding client: {e}")
                        
            self._initialized = True
            logging.info("Azure Search RAG retriever initialized successfully")
            
        except Exception as e:
            logging.error(f"Failed to initialize Azure Search RAG retriever: {e}")
            raise
    
    async def _get_embedding(self, text: str) -> List[float]:
        """Generate embedding for the given text."""
        if not self.embedding_client:
            return None
            
        try:
            embedding_dependency = self.settings.azure_openai.extract_embedding_dependency()
            deployment_name = embedding_dependency.get("deployment_name")
            
            response = await self.embedding_client.embeddings.create(
                model=deployment_name,
                input=text
            )
            return response.data[0].embedding
        except Exception as e:
            logging.error(f"Failed to generate embedding: {e}")
            return None
    
    async def retrieve(self, query: str, top_k: int = 5) -> List[RAGDocument]:
        """Retrieve relevant documents from Azure Search."""
        await self._initialize()
        
        try:
            from azure.search.documents.models import VectorizedQuery
            
            search_params = {
                "search_text": query if self.settings.datasource.query_type in ["simple", "semantic", "vector_simple_hybrid", "vector_semantic_hybrid"] else None,
                "top": top_k,
                "include_total_count": True
            }
            
            # Add vector search if required
            if self.settings.datasource.query_type in ["vector", "vector_simple_hybrid", "vector_semantic_hybrid"]:
                query_embedding = await self._get_embedding(query)
                if query_embedding and self.settings.datasource.vector_columns:
                    vector_queries = []
                    for vector_field in self.settings.datasource.vector_columns:
                        vector_queries.append(VectorizedQuery(
                            vector=query_embedding,
                            k_nearest_neighbors=top_k,
                            fields=vector_field
                        ))
                    search_params["vector_queries"] = vector_queries
            
            # Add semantic search configuration
            if self.settings.datasource.query_type in ["semantic", "vector_semantic_hybrid"]:
                if self.settings.datasource.semantic_search_config:
                    search_params["query_type"] = "semantic"
                    search_params["semantic_configuration_name"] = self.settings.datasource.semantic_search_config
            
            # Execute search
            results = await self.search_client.search(**search_params)
            
            # Convert results to RAGDocument objects
            documents = []
            async for result in results:
                content_parts = []
                
                # Extract content from configured content fields
                if self.settings.datasource.content_columns:
                    for field in self.settings.datasource.content_columns:
                        if field in result and result[field]:
                            content_parts.append(str(result[field]))
                else:
                    # Fallback to common field names
                    for field in ["content", "text", "body"]:
                        if field in result and result[field]:
                            content_parts.append(str(result[field]))
                            break
                
                if not content_parts:
                    continue
                
                content = "\n".join(content_parts)
                title = result.get(self.settings.datasource.title_column) if self.settings.datasource.title_column else None
                url = result.get(self.settings.datasource.url_column) if self.settings.datasource.url_column else None
                filename = result.get(self.settings.datasource.filename_column) if self.settings.datasource.filename_column else None
                score = result.get("@search.score")
                
                doc = RAGDocument(
                    content=content,
                    title=title,
                    url=url,
                    filename=filename,
                    score=score,
                    chunk_id=result.get("id", str(len(documents)))
                )
                documents.append(doc)
            
            logging.info(f"Retrieved {len(documents)} documents from Azure Search")
            return documents
            
        except Exception as e:
            logging.error(f"Error retrieving documents from Azure Search: {e}")
            return []
    
    async def close(self):
        """Clean up resources."""
        if self.search_client:
            await self.search_client.close()
        if self.embedding_client:
            await self.embedding_client.close()


class RAGService:
    """Main RAG service that handles document retrieval and context injection."""
    
    def __init__(self, settings):
        self.settings = settings
        self.retriever = None
        self._initialize_retriever()
    
    def _initialize_retriever(self):
        """Initialize the appropriate retriever based on datasource type."""
        if not self.settings.datasource:
            logging.warning("No datasource configured - RAG service will not be available")
            return
        
        datasource_type = self.settings.base_settings.datasource_type
        
        if datasource_type == "AzureCognitiveSearch":
            self.retriever = AzureSearchRAGRetriever(self.settings)
        else:
            logging.warning(f"RAG retriever not implemented for datasource type: {datasource_type}")
    
    async def retrieve_context(self, query: str, top_k: int = None) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Retrieve relevant context for the given query.
        
        Returns:
            Tuple of (formatted_context, citations)
        """
        if not self.retriever:
            return "", []
        
        try:
            # Use configured top_k or default
            if top_k is None:
                top_k = getattr(self.settings.datasource, 'top_k', 5)
            
            documents = await self.retriever.retrieve(query, top_k)
            
            if not documents:
                return "", []
            
            # Format context
            context_parts = []
            citations = []
            
            for i, doc in enumerate(documents):
                # Add to context
                context_parts.append(f"[{i+1}] {doc.content}")
                
                # Add to citations
                citation = doc.to_citation_dict()
                citation["id"] = str(i + 1)
                citations.append(citation)
            
            formatted_context = "\n\n".join(context_parts)
            
            logging.info(f"Retrieved context with {len(documents)} documents for query")
            return formatted_context, citations
            
        except Exception as e:
            logging.error(f"Error retrieving context: {e}")
            return "", []
    
    def format_context_for_prompt(self, context: str, query: str) -> str:
        """Format the retrieved context for injection into the prompt."""
        if not context:
            return query
        
        # Use a format that's compatible with a configured system message
        # The system message expects RAG context blocks, so we format accordingly
        formatted_prompt = f"""The following information was retrieved from internal documents and may be relevant to your query:

---
{context}
---

User Query: {query}

Please answer based on the retrieved information above and your knowledge as the D&A Software Architecture Assistant."""
        
        return formatted_prompt
    
    async def close(self):
        """Clean up resources."""
        if self.retriever:
            await self.retriever.close()


# Global RAG service instance
rag_service = None


async def init_rag_service(settings):
    """Initialize the global RAG service."""
    global rag_service
    try:
        rag_service = RAGService(settings)
        logging.info("RAG service initialized successfully")
    except Exception as e:
        logging.error(f"Failed to initialize RAG service: {e}")
        rag_service = None


async def get_rag_service() -> Optional[RAGService]:
    """Get the global RAG service instance."""
    return rag_service
